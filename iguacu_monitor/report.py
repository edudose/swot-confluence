"""Lectura del estado de la cuenca a partir de lo acumulado."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from iguacu_monitor.config import Config, Site
from iguacu_monitor.model import PRECIP, QAFL, QDEF, QOBS, QVER

# Los pluviómetros de ANA reportan cada 15 min con ~1 h de demora: más de
# 3 h sin lecturas es una actualización perdida.
RAIN_STALE_H = 3

ACCUMULATION_WINDOWS = (1, 3, 7, 30)  # días

# Estados de una usina, del más grave al más leve.
ALERTA, ATENCION, NORMAL, NIVEL, SIN_DATO = "alerta", "atencion", "normal", "nivel", "sin_dato"


def slope_per_hour(points: list[tuple[datetime, float]]) -> float | None:
    """Pendiente por mínimos cuadrados, en unidades por hora.

    Más estable que la diferencia entre las dos últimas lecturas, que salta
    con cualquier lectura ruidosa de la telemetría.
    """
    if len(points) < 2:
        return None
    t_end = points[-1][0]
    xs = [(t - t_end).total_seconds() / 3600 for t, _ in points]
    ys = [v for _, v in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx


@dataclass
class SiteState:
    key: str
    name: str
    kind: str
    values: dict[str, float] = field(default_factory=dict)
    last_seen: datetime | None = None
    age_hours: float | None = None
    site: Site | None = None
    trend_per_h: float | None = None              # de la variable principal
    series: list[tuple[datetime, float]] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        """Nombre sin el paréntesis oficial, que no entra en la tabla."""
        return self.name.split(" (")[0].strip()

    @property
    def headline_var(self) -> str:
        """La variable que encabeza la tarjeta: lo que sale, o lo que pasa."""
        return QDEF if self.kind == "plant" else QOBS

    @property
    def headline(self) -> float | None:
        return self.values.get(self.headline_var)

    @property
    def status(self) -> str | None:
        """Estado según el caudal que ENTRA, como fija la IO-OR.PR.IGU.

        Sólo las usinas tienen estado; las estaciones devuelven None.
        """
        if self.kind != "plant":
            return None
        if self.site is None:
            return SIN_DATO
        if self.site.operation == "nivel":
            return NIVEL
        inflow = self.values.get(QAFL)
        if inflow is None:
            return SIN_DATO
        if inflow >= self.site.alert:
            return ALERTA
        if inflow >= self.site.attention:
            return ATENCION
        return NORMAL

    def trend_direction(self, flat_band: float) -> str:
        """`up`, `down` o `flat` según una banda muerta en m3/s por hora."""
        if self.trend_per_h is None:
            return "flat"
        if self.trend_per_h > flat_band:
            return "up"
        if self.trend_per_h < -flat_band:
            return "down"
        return "flat"

    @property
    def peak(self) -> tuple[datetime, float] | None:
        """Máximo de la ventana de la sparkline, con su instante.

        Ante un empate gana el instante más reciente: es el que interesa
        cuando el caudal se mantiene en su pico.
        """
        if not self.series:
            return None
        return max(self.series, key=lambda p: (p[1], p[0]))

    @property
    def stale(self) -> bool:
        """Sin dato reciente. Seis horas es laxo para telemetría horaria."""
        return self.age_hours is None or self.age_hours > 6

    @property
    def spilling(self) -> bool:
        return self.values.get(QVER, 0) > 0


@dataclass
class GaugeRain:
    """Lluvia medida por los pluviómetros en una ventana.

    No es una media areal: los pluviómetros no se reparten parejo sobre la
    cuenca. La mediana es la cifra central, robusta a un sensor con falla.
    """

    window_h: int
    n: int
    median: float
    mean: float
    p90: float
    max_value: float
    max_name: str
    # (nombre, total crudo en la ventana, motivo)
    excluded: list[tuple[str, float, str]] = field(default_factory=list)
    until: datetime | None = None                      # fin de la ventana
    by_network: dict[str, int] = field(default_factory=dict)
    sites: frozenset[str] = frozenset()                # pluviómetros que entraron


@dataclass
class WindowTotals:
    """Totales por pluviómetro en una ventana, ya filtrados."""

    totals: object                                     # Serie sitio -> mm
    excluded: list[tuple[str, float, str]]
    until: datetime | None = None                      # última lectura usada


IMPLAUSIBLE = "lluvia por encima de lo físicamente plausible"
ISOLATED = "total aislado"
DRY = "seco entre vecinos con lluvia"


def network_of(key: str) -> str:
    return "CEMADEN" if str(key).startswith("cem_") else "ANA"


def _dry_gauges(totals, coords, radius_km: float, min_neighbors: int,
                neighbor_mm: float, frac: float) -> list[str]:
    """Pluviómetros que marcan casi nada mientras sus vecinos sí miden lluvia."""
    import numpy as np

    keys = [k for k in totals.index if k in coords]
    if len(keys) <= min_neighbors:
        return []
    lon = np.array([coords[k][0] for k in keys])
    lat = np.array([coords[k][1] for k in keys])
    vals = totals.reindex(keys).to_numpy(dtype=float)
    dx = (lon[:, None] - lon[None, :]) * 111.2 * np.cos(np.radians(lat.mean()))
    dy = (lat[:, None] - lat[None, :]) * 111.2
    near = (np.hypot(dx, dy) <= radius_km) & ~np.eye(len(keys), dtype=bool)
    out = []
    for i, key in enumerate(keys):
        neighbors = vals[near[i]]
        if len(neighbors) >= min_neighbors:
            med = float(np.median(neighbors))
            if med >= neighbor_mm and vals[i] < frac * med:
                out.append(key)
    return out


def gauge_window_totals(frame, cfg: Config) -> dict[int, WindowTotals]:
    """Totales por pluviómetro y ventana, ya filtrados por control de calidad.

    Cada ventana termina en la última lectura de su red. ANA da series cada
    15 min y entra en todas las ventanas. CEMADEN publica sólo el acumulado
    de 24 h: se suma a esa ventana cuando su corte y el de ANA están a menos
    de RAIN_STALE_H horas; si ANA quedó atrás, la ventana de 24 h usa sólo
    CEMADEN, y si quedó atrás CEMADEN, sólo ANA.

    1. Un pluviómetro que supera la envolvente plausible en alguna lectura
       o en las sumas móviles de 1 h y 3 h queda fuera de esa ventana:
       recortar sólo la lectura dejaría un total parcial igual de dudoso.
       El total de 24 h de CEMADEN se compara con la envolvente de 24 h.
    2. Un pluviómetro de ANA entra en la ventana sólo si tiene al menos
       `gauge_min_coverage` de las lecturas que su paso hace esperar.
    3. Un total aislado (z robusto > `gauge_outlier_z`) queda fuera del
       agregado y se informa: el estado de calidad de ANA no lo detecta.
    4. Un pluviómetro seco entre vecinos con lluvia (menos de
       `gauge_dry_frac` de la mediana de los vecinos a
       `gauge_dry_radius_km`, con al menos `gauge_dry_min_neighbors` y esa
       mediana sobre `gauge_dry_neighbor_mm`) es un sensor tapado o una
       estación sin pluviómetro que transmite ceros: caso real del 24/10/2026,
       12 estaciones en 0 mm con vecinos de 70 a 140 mm. Se evalúa en
       ventanas de `gauge_dry_min_window_h` o más; el que falla en 24 h o más
       queda fuera de todas.
    El dato crudo no se modifica; el control se aplica al leer.
    """
    import numpy as np
    import pandas as pd

    from iguacu_monitor.model import PRECIP_24H

    opts = cfg.report
    windows = [int(w) for w in opts.get("gauge_windows_h", (1, 3, 6, 24, 72))]
    min_cov = float(opts.get("gauge_min_coverage", 0.8))
    max_1h = float(opts.get("gauge_max_1h_mm", 100))

    def envelope(hours: float) -> float:
        return max_1h * hours ** 0.5
    z_cut = float(opts.get("gauge_outlier_z", 6))
    names = {g.key: g.name for g in cfg.gauges}
    coords = {g.key: (g.lon, g.lat) for g in cfg.gauges if g.lon is not None and g.lat is not None}

    # ventana -> [totales, excluidos, corte]
    stage: dict[int, list] = {}
    ref = None
    rain = frame[frame["site"].astype(str).str.startswith("pluv_") & (frame["variable"] == PRECIP)]
    if not rain.empty:
        rain = rain.sort_values(["site", "timestamp"])
        step_h = (rain.groupby("site")["timestamp"]
                      .apply(lambda s: s.diff().median().total_seconds() / 3600 if len(s) > 1 else np.nan))
        rain = rain.assign(step_h=rain["site"].map(step_h))
        rain = rain[rain["step_h"].notna()]
    if not rain.empty:
        by_site = rain.set_index("timestamp").groupby("site")["value"]
        sum_1h = by_site.rolling("60min").sum().reset_index(level=0, drop=True).to_numpy()
        sum_3h = by_site.rolling("180min").sum().reset_index(level=0, drop=True).to_numpy()
        rain = rain.assign(impossible=(rain["value"] > envelope(1) * rain["step_h"] ** 0.5)
                           | (sum_1h > envelope(1)) | (sum_3h > envelope(3)))
        ref = rain["timestamp"].max()
        for window in windows:
            sel = rain[rain["timestamp"] > ref - pd.Timedelta(hours=window)]
            counts = sel.groupby("site")["value"].count()
            expected = window / step_h.reindex(counts.index)
            totals = sel.groupby("site")["value"].sum()[counts >= min_cov * expected.clip(lower=1)]
            if totals.empty:
                continue
            bad = sorted(set(sel.loc[sel["impossible"], "site"]) & set(totals.index))
            excluded = [(names.get(site, site), float(totals[site]), IMPLAUSIBLE) for site in bad]
            stage[window] = [totals.drop(index=bad), excluded, ref]

    cem = frame[frame["site"].astype(str).str.startswith("cem_") & (frame["variable"] == PRECIP_24H)]
    if 24 in windows and not cem.empty:
        cref = cem["timestamp"].max()
        snap = cem[cem["timestamp"] == cref].groupby("site")["value"].last()
        gap = None if ref is None else (cref - ref).total_seconds() / 3600
        base = stage.get(24) if gap is not None and gap <= RAIN_STALE_H else None
        if gap is None or gap >= -RAIN_STALE_H:
            excluded = list(base[1]) if base else []
            bad = snap[snap > envelope(24)]
            excluded += [(names.get(site, site), float(v), IMPLAUSIBLE) for site, v in bad.items()]
            snap = snap.drop(index=bad.index)
            totals = pd.concat([base[0], snap]) if base else snap
            stage[24] = [totals, excluded, max(ref, cref) if base else cref]

    for window, st in stage.items():
        totals = st[0]
        if len(totals) >= 8:
            med = totals.median()
            # Piso de 10 mm para la dispersión: con la cuenca seca la MAD es
            # cero y cualquier tormenta local parecería una falla. Con el
            # piso, un total tiene que superar la mediana en ~90 mm para
            # quedar fuera, y lo excluido siempre se informa.
            scale = max(float((totals - med).abs().median()), 10.0)
            z = 0.6745 * (totals - med) / scale
            st[1] += [(names.get(site, site), float(totals[site]), ISOLATED) for site in totals[z > z_cut].index]
            st[0] = totals[z <= z_cut]

    dry_args = (float(opts.get("gauge_dry_radius_km", 50)), int(opts.get("gauge_dry_min_neighbors", 3)),
                float(opts.get("gauge_dry_neighbor_mm", 20)), float(opts.get("gauge_dry_frac", 0.25)))
    min_dry_w = int(opts.get("gauge_dry_min_window_h", 6))
    dry_by_window = {w: set(_dry_gauges(st[0], coords, *dry_args)) for w, st in stage.items() if w >= min_dry_w}
    dry_always = set().union(*[d for w, d in dry_by_window.items() if w >= 24])
    for window, st in stage.items():
        totals = st[0]
        flagged = sorted((dry_by_window.get(window, set()) | dry_always) & set(totals.index))
        st[1] += [(names.get(site, site), float(totals[site]), DRY) for site in flagged]
        st[0] = totals.drop(index=flagged)

    return {w: WindowTotals(totals=t, excluded=e, until=u.to_pydatetime() if u is not None else None)
            for w, (t, e, u) in sorted(stage.items()) if not t.empty}


def gauge_rain(frame, cfg: Config, outside: set[str] | None = None,
               totals_by_window=None) -> dict[int, GaugeRain]:
    """Estadísticas por ventana de la lluvia medida.

    `outside` son los pluviómetros que se sabe que quedan fuera de la cuenca:
    sirven para interpolar en los bordes, no para el resumen.
    """
    names = {g.key: g.name for g in cfg.gauges}
    per_window = totals_by_window if totals_by_window is not None else gauge_window_totals(frame, cfg)
    out: dict[int, GaugeRain] = {}
    for window, wt in per_window.items():
        totals = wt.totals
        if outside:
            totals = totals[~totals.index.isin(outside)]
        if totals.empty:
            continue
        top = totals.idxmax()
        nets: dict[str, int] = {}
        for key in totals.index:
            nets[network_of(key)] = nets.get(network_of(key), 0) + 1
        out[window] = GaugeRain(
            window_h=window, n=int(len(totals)),
            median=float(totals.median()), mean=float(totals.mean()),
            p90=float(totals.quantile(0.9)),
            max_value=float(totals.max()), max_name=names.get(top, top),
            excluded=wt.excluded, until=wt.until, by_network=nets,
            sites=frozenset(totals.index),
        )
    return out


@dataclass
class SubBasinRain:
    """Lluvia sobre un área incremental de la cascada."""

    id: str
    name: str
    order: int
    area_km2: float
    n_gauges: int                                          # pluviómetros dentro
    gauge_mm: dict[int, float] = field(default_factory=dict)   # IDW, por ventana en h
    merge_mm: dict[int, float] = field(default_factory=dict)   # MERGE, por días


def subbasin_rain(frame, cfg: Config, now: datetime, totals_by_window) -> tuple[list[SubBasinRain], set[str]]:
    """Lluvia por subcuenca: pluviómetros interpolados (IDW) y MERGE.

    Devuelve (subcuencas, claves de pluviómetros que quedan fuera de la cuenca). Los
    pluviómetros se interpolan a una grilla de `subbasin_grid_deg` y se
    promedian por polígono, ponderando por área de celda; los que caen hasta
    ~10 km fuera del contorno entran en la interpolación para no subestimar
    los bordes. Sin capa de subcuencas devuelve listas vacías.
    """
    import numpy as np
    import pandas as pd
    from shapely import contains_xy

    from iguacu_monitor import subbasins

    subs = subbasins.load(cfg.subbasins_path) if cfg.subbasins_path else []
    if not subs:
        return [], set()
    opts = cfg.report
    windows = [int(w) for w in opts.get("subbasin_windows_h", (6, 24, 72))]
    step = float(opts.get("subbasin_grid_deg", 0.05))

    contour = subbasins.outline(subs)
    near = contour.buffer(0.1)
    snug = contour.buffer(0.02)
    coords = {g.key: (g.lon, g.lat) for g in cfg.gauges if g.lon is not None and g.lat is not None}
    near_keys = {k for k, (x, y) in coords.items() if contains_xy(near, x, y)}
    inside_keys = {k for k, (x, y) in coords.items() if contains_xy(snug, x, y)}

    minx, miny, maxx, maxy = contour.bounds
    lons = np.arange(minx + step / 2, maxx, step)
    lats = np.arange(miny + step / 2, maxy, step)
    masks = subbasins.grid_masks(lons, lats, subs)
    weights = np.cos(np.radians(lats))[:, None] * np.ones((1, len(lons)))

    # Pluviómetros con dato válido en 24 h (o en la ventana más larga) dentro
    # de cada subcuenca: es la medida de confianza del valor interpolado.
    ref_w = 24 if 24 in totals_by_window else (max(totals_by_window) if totals_by_window else None)
    with_data = set(totals_by_window[ref_w].totals.index) if ref_w is not None else set()
    results = {s.id: SubBasinRain(id=s.id, name=s.name, order=s.order, area_km2=s.area_km2,
                                  n_gauges=sum(1 for k in inside_keys & with_data
                                               if contains_xy(s.geom, *coords[k])))
               for s in subs}

    for w in windows:
        if w not in totals_by_window:
            continue
        totals = totals_by_window[w].totals
        totals = totals[totals.index.isin(near_keys)]
        if len(totals) < 3:
            continue
        gx = [coords[k][0] for k in totals.index]
        gy = [coords[k][1] for k in totals.index]
        grid = subbasins.idw_grid(gx, gy, totals.values, lons, lats)
        for s in subs:
            m = masks[s.id]
            if m.any():
                results[s.id].gauge_mm[w] = float((grid[m] * weights[m]).sum() / weights[m].sum())

    # MERGE se publica con un día de rezago: las ventanas cuentan desde el
    # último día publicado, no desde ahora, para no quedar vacías.
    rain = frame[frame["variable"] == PRECIP]
    for s in subs:
        sel = rain[rain["site"] == s.key]
        if sel.empty:
            continue
        last = sel["timestamp"].max()
        for days in (1, 3):
            part = sel[sel["timestamp"] > last - timedelta(days=days)]
            results[s.id].merge_mm[days] = float(part["value"].sum())

    return [results[s.id] for s in subs], set(coords) - inside_keys


@dataclass
class Arrival:
    """Caudal que pasó por un punto aguas arriba y cuándo llega al destino."""

    name: str
    variable: str          # qdef (lo que sale de una usina) o qobs (estación)
    q: float
    seen: datetime         # cuándo pasó por ese punto
    lag_h: float           # tiempo de viaje acumulado hasta el destino (configurado)
    lag_alt_h: float | None = None   # con el último tramo estimado de los datos

    @property
    def eta(self) -> datetime:
        return self.seen + timedelta(hours=self.lag_h)

    @property
    def eta_alt(self) -> datetime | None:
        return self.seen + timedelta(hours=self.lag_alt_h) if self.lag_alt_h is not None else None


@dataclass
class Wave:
    """Onda hacia un punto del tronco por traslado de lo ya observado aguas arriba.

    No es un pronóstico: no incluye lluvia futura ni aporte lateral entre
    puntos, sólo traslada en el tiempo lo que ya pasó por cada punto.
    """

    target: str
    target_q: float | None
    last_leg: str                      # punto inmediatamente aguas arriba
    lag_cfg_h: float
    lag_est_h: float | None = None     # desfase estimado del último tramo
    lag_r: float | None = None
    front_seen: datetime | None = None  # inicio del ascenso aguas arriba
    front_lag_h: float | None = None
    rise_q: float | None = None         # cuánto subió aguas arriba desde el inicio
    arrivals: list[Arrival] = field(default_factory=list)

    @property
    def rising(self) -> bool:
        return self.front_seen is not None


def estimate_lag(up: list[tuple[datetime, float]], down: list[tuple[datetime, float]],
                 around_h: float, window_h: float = 48) -> tuple[float | None, float | None]:
    """Desfase (h) que maximiza la correlación entre dos series horarias.

    Sólo se acepta si la serie de aguas arriba varió (> 2 % de su media) y la
    correlación es alta (r >= 0,9): con caudales planos el desfase no está
    definido y cualquier cifra sería ruido.
    """
    import pandas as pd

    if len(up) < 12 or len(down) < 12:
        return None, None
    u = pd.Series(dict(up)).sort_index().resample("1h").mean().interpolate(limit=3)
    d = pd.Series(dict(down)).sort_index().resample("1h").mean().interpolate(limit=3)
    end = min(u.index.max(), d.index.max())
    u = u[u.index > end - pd.Timedelta(hours=window_h + around_h + 6)]
    d = d[d.index > end - pd.Timedelta(hours=window_h)]
    if u.std() < 0.02 * u.mean():
        return None, None
    best: tuple[float, int] | None = None
    for lag in range(max(1, int(around_h) - 5), int(around_h) + 6):
        r = d.corr(u.shift(lag, freq="1h").reindex(d.index))
        if r == r and (best is None or r > best[0]):
            best = (float(r), lag)
    if best is None or best[0] < 0.9:
        return None, None
    return float(best[1]), round(best[0], 3)


def wave_to(target_key: str, states: list[SiteState], cfg: Config, max_points: int = 2) -> Wave | None:
    by_key = {s.key: s for s in states}
    chain = [p.key for p in cfg.plants] + list(cfg.report.get("panel_stations") or [])
    if target_key not in chain:
        return None
    target = by_key.get(target_key)
    if target is None or target.site is None or target.site.travel_h is None:
        return None
    i = chain.index(target_key)
    up_key = chain[i - 1]
    up = by_key.get(up_key)
    wave = Wave(target=target.display_name, target_q=target.headline,
                last_leg=up.display_name if up else up_key, lag_cfg_h=target.site.travel_h)

    if up and up.series and target.series:
        wave.lag_est_h, wave.lag_r = estimate_lag(up.series, target.series, target.site.travel_h)

    # Traslado desde los puntos aguas arriba más cercanos, mientras se conozca
    # el tiempo de viaje de cada tramo.
    lag = 0.0
    for j in range(i, 0, -1):
        leg = by_key.get(chain[j])
        if leg is None or leg.site is None or leg.site.travel_h is None:
            break
        lag += leg.site.travel_h
        src = by_key.get(chain[j - 1])
        if src is None or src.headline is None or src.last_seen is None:
            continue
        alt = lag - wave.lag_cfg_h + wave.lag_est_h if wave.lag_est_h is not None else None
        wave.arrivals.append(Arrival(name=src.display_name, variable=src.headline_var,
                                     q=src.headline, seen=src.last_seen, lag_h=lag, lag_alt_h=alt))
        if len(wave.arrivals) >= max_points:
            break

    # Inicio del ascenso en el punto inmediato aguas arriba: el último dato
    # todavía pegado al mínimo de las últimas 24 h (dentro de 2 %), si desde
    # entonces subió de forma clara. Tomar el mínimo a secas cae en cualquier
    # punto de la meseta previa, a veces horas antes de que empiece a subir.
    if up and up.series:
        recent = [p for p in up.series if p[0] >= up.series[-1][0] - timedelta(hours=24)]
        q_min = min(q for _, q in recent)
        q_now = recent[-1][1]
        if q_now - q_min >= max(0.05 * q_min, 300):
            flat = [t for t, q in recent if q <= q_min * 1.02]
            wave.front_seen = max(flat)
            wave.front_lag_h = wave.lag_cfg_h
            wave.rise_q = q_now - q_min
    return wave


@dataclass
class Report:
    generated: datetime
    basin: str
    sites: list[SiteState]
    precip: dict[int, float]
    notes: list[str] = field(default_factory=list)
    gauges: dict[int, GaugeRain] = field(default_factory=dict)
    subbasins: list[SubBasinRain] = field(default_factory=list)
    wave: Wave | None = None
    rain_until: datetime | None = None     # última lectura de los pluviómetros
    merge_until: datetime | None = None    # fin del último día MERGE publicado

    @property
    def rain_age_hours(self) -> float | None:
        if self.rain_until is None:
            return None
        return (self.generated - self.rain_until).total_seconds() / 3600

    @property
    def rain_stale(self) -> bool:
        """Las ventanas de lluvia terminan en la última lectura, no en la
        hora de consulta: si ANA dejó de responder hay que decirlo."""
        age = self.rain_age_hours
        return age is not None and age > RAIN_STALE_H

    @property
    def plants(self) -> list[SiteState]:
        return [s for s in self.sites if s.kind == "plant"]

    @property
    def stations(self) -> list[SiteState]:
        return [s for s in self.sites if s.kind == "station"]

    @property
    def spilling(self) -> list[SiteState]:
        return [s for s in self.plants if s.spilling]

    def by_status(self, status: str) -> list[SiteState]:
        return [s for s in self.plants if s.status == status]


def build(frame, cfg: Config, now: datetime | None = None) -> Report:
    import pandas as pd

    now = now or datetime.now(timezone.utc)
    notes: list[str] = []

    if frame.empty:
        return Report(generated=now, basin=cfg.basin.name, sites=[], precip={},
                      notes=["no hay datos almacenados todavía; corré `fetch` primero"])

    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)

    # --- último valor de cada (sitio, variable) -----------------------------
    latest = (frame.sort_values("timestamp")
                   .groupby(["site", "variable"], as_index=False)
                   .last())

    opts = cfg.report
    trend_window = timedelta(hours=float(opts.get("trend_window_h", 3)))
    spark_window = timedelta(hours=float(opts.get("sparkline_h", 48)))

    states: list[SiteState] = []
    for site in [*cfg.plants, *cfg.stations]:
        rows = latest[latest["site"] == site.key]
        if rows.empty:
            states.append(SiteState(key=site.key, name=site.name, kind=site.kind, site=site))
            continue
        values = {r["variable"]: float(r["value"]) for _, r in rows.iterrows()}
        last_seen = rows["timestamp"].max().to_pydatetime()
        state = SiteState(
            key=site.key, name=site.name, kind=site.kind, values=values,
            last_seen=last_seen,
            age_hours=(now - last_seen).total_seconds() / 3600,
            site=site,
        )

        # Serie de la variable principal: alimenta la tendencia y la sparkline.
        hist = frame[(frame["site"] == site.key) & (frame["variable"] == state.headline_var)]
        if not hist.empty:
            # Si dos fuentes reportan el mismo instante, promediarlas evita
            # que la sparkline zigzaguee entre ellas.
            hist = hist.groupby("timestamp", as_index=False)["value"].mean()
            hist = hist.sort_values("timestamp")
            end = hist["timestamp"].iloc[-1]
            spark = hist[hist["timestamp"] >= end - spark_window]
            state.series = [(t.to_pydatetime(), float(v))
                            for t, v in zip(spark["timestamp"], spark["value"])]
            recent = [p for p in state.series if p[0] >= end.to_pydatetime() - trend_window]
            state.trend_per_h = slope_per_hour(recent)
        states.append(state)

    # --- precipitación acumulada sobre la cuenca ----------------------------
    rain = frame[(frame["variable"] == PRECIP) & (frame["site"] == "basin")]
    precip: dict[int, float] = {}
    merge_until = rain["timestamp"].max().to_pydatetime() if not rain.empty else None
    for window in ACCUMULATION_WINDOWS:
        if merge_until is None:
            break
        sel = rain[rain["timestamp"] > merge_until - timedelta(days=window)]
        precip[window] = float(sel["value"].sum())

    if rain.empty:
        notes.append("sin precipitación de MERGE: ¿falta cfgrib o no se corrió `fetch`?")

    # --- coherencia de la cascada -------------------------------------------
    # Cada estación del tronco se compara con el punto anterior de la cadena
    # tal como estaba hace `travel_h` horas: en una crecida, lo que una usina
    # larga ahora llega aguas abajo recién después del tiempo de viaje, así
    # que comparar valores simultáneos da falsas alarmas en el ascenso.
    # Aguas abajo hay aporte incremental, no pérdida: caer muy por debajo del
    # aguas arriba desfasado sugiere un dato viejo o mal escalado.
    by_key = {s.key: s for s in states}
    chain = [p.key for p in cfg.plants] + list(opts.get("panel_stations") or [])
    for upstream_key, key in zip(chain, chain[1:]):
        down, up = by_key.get(key), by_key.get(upstream_key)
        if not down or not up or down.kind != "station" or not down.site:
            continue
        lag = down.site.travel_h
        q_now = down.headline
        if lag is None or q_now is None or not up.series or down.last_seen is None:
            continue
        target = down.last_seen - timedelta(hours=lag)
        earlier = [v for t, v in up.series if t <= target]
        if not earlier or up.series[0][0] > target:
            continue  # la serie aguas arriba no llega tan atrás
        q_up = earlier[-1]
        if q_up <= 0:
            continue
        ratio = q_now / q_up
        if ratio < 0.85:
            notes.append(
                f"{down.display_name} ({q_now:,.0f} m³/s) por debajo de "
                f"{up.display_name} hace {lag:g} h ({q_up:,.0f} m³/s): "
                f"revisar vigencia o escala de los datos")
        elif ratio > 2.5:
            notes.append(
                f"{down.display_name} ({q_now:,.0f}) más que duplica a "
                f"{up.display_name} hace {lag:g} h ({q_up:,.0f}): "
                f"aporte incremental alto o error de escala")

    for state in states:
        if state.stale and state.last_seen is not None:
            notes.append(f"{state.name}: último dato hace {state.age_hours:.0f} h")

    from iguacu_monitor.model import PRECIP_24H
    for net, prefix, var in (("ANA", "pluv_", PRECIP), ("CEMADEN", "cem_", PRECIP_24H)):
        sel = frame[frame["site"].astype(str).str.startswith(prefix) & (frame["variable"] == var)]
        if sel.empty:
            continue
        age = (now - sel["timestamp"].max().to_pydatetime()).total_seconds() / 3600
        if age > RAIN_STALE_H:
            notes.append(f"pluviómetros {net}: último dato hace {age:.0f} h")

    totals_by_window = gauge_window_totals(frame, cfg)
    subs, outside = subbasin_rain(frame, cfg, now, totals_by_window)
    gauges = gauge_rain(frame, cfg, outside=outside, totals_by_window=totals_by_window)
    ref_w = 24 if 24 in gauges else (max(gauges) if gauges else None)
    rain_until = gauges[ref_w].until if ref_w is not None else None
    flagged: dict[str, tuple[int, float, str]] = {}
    for w in sorted(gauges):
        for name, total, reason in gauges[w].excluded:
            flagged[name] = (w, total, reason)          # queda la ventana más larga
    dry = sorted(name for name, (_, _, reason) in flagged.items() if reason == DRY)
    if dry:
        notes.append(f"{len(dry)} pluviómetro(s) excluidos por marcar casi nada con lluvia "
                     f"alrededor (sensor tapado o sin pluviómetro): {', '.join(dry)}")
    for name, (w, total, reason) in flagged.items():
        if reason != DRY:
            notes.append(f"pluviómetro excluido de la lluvia medida: {name} "
                         f"({reason}; {total:,.0f} mm en {w} h)")

    target = (cfg.report.get("panel_stations") or [None])[-1]
    wave = wave_to(target, states, cfg) if target else None

    return Report(generated=now, basin=cfg.basin.name, sites=states,
                  precip=precip, notes=notes, gauges=gauges, subbasins=subs, wave=wave,
                  rain_until=rain_until, merge_until=merge_until)


def render_text(report: Report) -> str:
    from iguacu_monitor.model import QAFL, QDEF, QTUR, QVER

    name_w, num_w = 26, 11

    def cell(state: SiteState, var: str) -> str:
        value = state.values.get(var)
        return f"{value:>{num_w},.0f}" if value is not None else f"{'—':>{num_w}}"

    lines = [
        f"Monitor {report.basin} — {report.generated:%Y-%m-%d %H:%M} UTC",
        "=" * 72,
        "",
        "REGULACIÓN (m³/s)",
        f"  {'usina':<{name_w}}" + "".join(
            f"{h:>{num_w}}" for h in ("afluente", "turbinada", "vertida", "defluente")),
    ]
    for state in report.plants:
        flag = "  ⚠" if (state.stale and state.last_seen) else ""
        lines.append(
            f"  {state.display_name[:name_w]:<{name_w}}"
            + "".join(cell(state, v) for v in (QAFL, QTUR, QVER, QDEF))
            + flag
        )

    lines += ["", "CAUDAL OBSERVADO (m³/s)",
              f"  {'estación':<{name_w}}{'caudal':>{num_w}}   antigüedad"]
    for state in report.stations:
        age = f"hace {state.age_hours:.0f} h" if state.age_hours is not None else "sin dato"
        lines.append(f"  {state.display_name[:name_w]:<{name_w}}"
                     f"{cell(state, QOBS)}   {age}")

    if report.gauges:
        lines += ["", "LLUVIA MEDIDA (mm, pluviómetros ANA y CEMADEN; no es media areal)",
                  f"  {'ventana':<{name_w}}{'n':>5}{'mediana':>{num_w}}{'media':>{num_w}}"
                  f"{'p90':>{num_w}}{'máx':>{num_w}}  hasta (UTC)   redes · dónde"]
        for w in sorted(report.gauges):
            g = report.gauges[w]
            until = f"{g.until:%d %H:%M}" if g.until else "—"
            nets = " + ".join(f"{k} {v}" for k, v in sorted(g.by_network.items()))
            lines.append(f"  {f'últimas {w} h':<{name_w}}{g.n:>5}{g.median:>{num_w},.1f}"
                         f"{g.mean:>{num_w},.1f}{g.p90:>{num_w},.1f}{g.max_value:>{num_w},.1f}"
                         f"  {until:<12}  {nets} · {g.max_name}")

    if report.wave and report.wave.arrivals:
        w = report.wave
        local = timezone(timedelta(hours=-3))
        est = (f"; estimado con 48 h: {w.lag_est_h:g} h (r {w.lag_r:.2f})" if w.lag_est_h is not None else "")
        lines += ["", f"ONDA HACIA {w.target.upper()} (traslado de lo observado; sin lluvia futura ni aporte lateral)",
                  f"  tiempo de viaje {w.last_leg}→{w.target}: {w.lag_cfg_h:g} h configurado{est}"]
        if w.rising:
            eta = w.front_seen + timedelta(hours=w.front_lag_h)
            lines.append(f"  ascenso: empezó en {w.last_leg} {w.front_seen.astimezone(local):%d %H:%M} "
                         f"-> llega ≈ {eta.astimezone(local):%d %H:%M} (hora local)")
        for a in w.arrivals:
            what = "sale de" if a.variable == QDEF else "pasa por"
            alt = f" / {a.eta_alt.astimezone(local):%d %H:%M}" if a.eta_alt else ""
            lines.append(f"  {a.q:>8,.0f} m³/s que {what} {a.name:<16} llega ≈ "
                         f"{a.eta.astimezone(local):%d %H:%M}{alt}  (+{a.lag_h:g} h)")

    if report.subbasins:
        ws = sorted({w for sb in report.subbasins for w in sb.gauge_mm})
        lines += ["", "LLUVIA POR SUBCUENCA (mm; pluviómetros interpolados y MERGE diario)",
                  f"  {'área incremental':<{name_w}}{'km²':>8}{'pluv':>6}"
                  + "".join(f"{f'{w} h':>8}" for w in ws) + f"{'MERGE 1d':>10}{'3d':>7}"]
        for sb in report.subbasins:
            cells = "".join(f"{sb.gauge_mm[w]:>8,.1f}" if w in sb.gauge_mm else f"{'—':>8}" for w in ws)
            m1 = f"{sb.merge_mm[1]:>10,.1f}" if 1 in sb.merge_mm else f"{'—':>10}"
            m3 = f"{sb.merge_mm[3]:>7,.1f}" if 3 in sb.merge_mm else f"{'—':>7}"
            lines.append(f"  {sb.name[:name_w]:<{name_w}}{sb.area_km2:>8,.0f}{sb.n_gauges:>6}{cells}{m1}{m3}")

    lines += ["", "PRECIPITACIÓN MEDIA AREAL (mm, MERGE)"]
    if report.precip:
        for window in sorted(report.precip):
            label = f"últimos {window} d"
            lines.append(f"  {label:<{name_w}}{report.precip[window]:>{num_w},.1f}")
    else:
        lines.append("  sin datos")

    observations = list(report.notes)
    if report.spilling:
        observations.append("vertiendo: " + ", ".join(s.display_name for s in report.spilling))
    if observations:
        lines += ["", "OBSERVACIONES"]
        lines += [f"  · {n}" for n in observations]
    return "\n".join(lines)
