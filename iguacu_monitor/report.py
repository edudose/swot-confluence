"""Lectura del estado de la cuenca a partir de lo acumulado."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from iguacu_monitor.config import Config, Site
from iguacu_monitor.model import PRECIP, QAFL, QDEF, QOBS, QVER

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


def gauge_rain(frame, cfg: Config) -> dict[int, GaugeRain]:
    """Totales por ventana de los pluviómetros, con control de calidad.

    1. Un pluviómetro que supera la envolvente plausible en alguna lectura
       o en las sumas móviles de 1 h y 3 h queda fuera de esa ventana:
       recortar sólo la lectura dejaría un total parcial igual de dudoso.
    2. Un pluviómetro entra en la ventana sólo si tiene al menos
       `gauge_min_coverage` de las lecturas que su paso hace esperar.
    3. Un total aislado (z robusto > `gauge_outlier_z`) queda fuera del
       agregado y se informa: el estado de calidad de ANA no lo detecta.
    El dato crudo no se modifica; el control se aplica al leer.
    """
    import numpy as np
    import pandas as pd

    opts = cfg.report
    windows = [int(w) for w in opts.get("gauge_windows_h", (1, 3, 6, 24, 72))]
    min_cov = float(opts.get("gauge_min_coverage", 0.8))
    max_1h = float(opts.get("gauge_max_1h_mm", 100))

    def envelope(hours: float) -> float:
        return max_1h * hours ** 0.5
    z_cut = float(opts.get("gauge_outlier_z", 6))
    names = {g.key: g.name for g in cfg.gauges}

    rain = frame[frame["site"].astype(str).str.startswith("pluv_") & (frame["variable"] == PRECIP)]
    if rain.empty:
        return {}
    rain = rain.sort_values(["site", "timestamp"])
    step_h = (rain.groupby("site")["timestamp"]
                  .apply(lambda s: s.diff().median().total_seconds() / 3600 if len(s) > 1 else np.nan))
    rain = rain.assign(step_h=rain["site"].map(step_h))
    rain = rain[rain["step_h"].notna()]
    if rain.empty:
        return {}
    by_site = rain.set_index("timestamp").groupby("site")["value"]
    sum_1h = by_site.rolling("60min").sum().reset_index(level=0, drop=True).to_numpy()
    sum_3h = by_site.rolling("180min").sum().reset_index(level=0, drop=True).to_numpy()
    rain = rain.assign(impossible=(rain["value"] > envelope(1) * rain["step_h"] ** 0.5)
                       | (sum_1h > envelope(1)) | (sum_3h > envelope(3)))
    ref = rain["timestamp"].max()

    out: dict[int, GaugeRain] = {}
    for window in windows:
        sel = rain[rain["timestamp"] > ref - pd.Timedelta(hours=window)]
        counts = sel.groupby("site")["value"].count()
        expected = window / step_h.reindex(counts.index)
        totals = sel.groupby("site")["value"].sum()[counts >= min_cov * expected.clip(lower=1)]
        if totals.empty:
            continue

        excluded: list[tuple[str, float, str]] = []
        bad = set(sel.loc[sel["impossible"], "site"]) & set(totals.index)
        for site in sorted(bad):
            excluded.append((names.get(site, site), float(totals[site]),
                             "lluvia por encima de lo físicamente plausible"))
        totals = totals.drop(index=sorted(bad))
        if totals.empty:
            continue
        if len(totals) >= 8:
            med = totals.median()
            # Piso de 10 mm para la dispersión: con la cuenca seca la MAD es
            # cero y cualquier tormenta local parecería una falla. Con el
            # piso, un total tiene que superar la mediana en ~90 mm para
            # quedar fuera, y lo excluido siempre se informa.
            scale = max(float((totals - med).abs().median()), 10.0)
            z = 0.6745 * (totals - med) / scale
            for site in totals[z > z_cut].index:
                excluded.append((names.get(site, site), float(totals[site]), "total aislado"))
            totals = totals[z <= z_cut]

        top = totals.idxmax()
        out[window] = GaugeRain(
            window_h=window, n=int(len(totals)),
            median=float(totals.median()), mean=float(totals.mean()),
            p90=float(totals.quantile(0.9)),
            max_value=float(totals.max()), max_name=names.get(top, top),
            excluded=excluded,
        )
    return out


@dataclass
class Report:
    generated: datetime
    basin: str
    sites: list[SiteState]
    precip: dict[int, float]
    notes: list[str] = field(default_factory=list)
    gauges: dict[int, GaugeRain] = field(default_factory=dict)

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
    for window in ACCUMULATION_WINDOWS:
        cutoff = now - timedelta(days=window)
        sel = rain[rain["timestamp"] >= cutoff]
        if not sel.empty:
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

    gauges = gauge_rain(frame, cfg)
    flagged: dict[str, tuple[int, float, str]] = {}
    for w in sorted(gauges):
        for name, total, reason in gauges[w].excluded:
            flagged[name] = (w, total, reason)          # queda la ventana más larga
    for name, (w, total, reason) in flagged.items():
        notes.append(f"pluviómetro excluido de la lluvia medida: {name} "
                     f"({reason}; {total:,.0f} mm en {w} h)")

    return Report(generated=now, basin=cfg.basin.name, sites=states,
                  precip=precip, notes=notes, gauges=gauges)


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
        lines += ["", "LLUVIA MEDIDA (mm, pluviómetros ANA; no es media areal)",
                  f"  {'ventana':<{name_w}}{'n':>5}{'mediana':>{num_w}}{'media':>{num_w}}"
                  f"{'p90':>{num_w}}{'máx':>{num_w}}  dónde"]
        for w in sorted(report.gauges):
            g = report.gauges[w]
            lines.append(f"  {f'últimas {w} h':<{name_w}}{g.n:>5}{g.median:>{num_w},.1f}"
                         f"{g.mean:>{num_w},.1f}{g.p90:>{num_w},.1f}{g.max_value:>{num_w},.1f}  {g.max_name}")

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
