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
class Report:
    generated: datetime
    basin: str
    sites: list[SiteState]
    precip: dict[int, float]
    notes: list[str] = field(default_factory=list)

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
    by_key = {s.key: s for s in states}
    caxias = by_key.get("salto_caxias")
    cataratas = by_key.get("hotel_cataratas")
    if caxias and cataratas:
        qdef = caxias.values.get(QDEF)
        qobs = cataratas.values.get(QOBS)
        if qdef and qobs:
            # Entre Salto Caxias y las Cataratas hay aporte incremental, así
            # que lo normal es que Cataratas supere a la defluencia. Lo
            # contrario sugiere un dato viejo o una lectura mal escalada.
            ratio = qobs / qdef
            if ratio < 0.9:
                notes.append(
                    f"Cataratas ({qobs:,.0f} m³/s) por debajo de la defluencia de "
                    f"Salto Caxias ({qdef:,.0f} m³/s): revisar vigencia de los datos")
            elif ratio > 2.5:
                notes.append(
                    f"Cataratas ({qobs:,.0f}) más que duplica Salto Caxias "
                    f"({qdef:,.0f}): aporte incremental alto o error de escala")

    for state in states:
        if state.stale and state.last_seen is not None:
            notes.append(f"{state.name}: último dato hace {state.age_hours:.0f} h")

    return Report(generated=now, basin=cfg.basin.name, sites=states,
                  precip=precip, notes=notes)


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
