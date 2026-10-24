"""Lectura del estado de la cuenca a partir de lo acumulado."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from iguacu_monitor.config import Config
from iguacu_monitor.model import PRECIP, QDEF, QOBS, QVER

ACCUMULATION_WINDOWS = (1, 3, 7, 30)  # días


@dataclass
class SiteState:
    key: str
    name: str
    kind: str
    values: dict[str, float] = field(default_factory=dict)
    last_seen: datetime | None = None
    age_hours: float | None = None

    @property
    def display_name(self) -> str:
        """Nombre sin el paréntesis oficial, que no entra en la tabla."""
        return self.name.split(" (")[0].strip()

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

    states: list[SiteState] = []
    for site in [*cfg.plants, *cfg.stations]:
        rows = latest[latest["site"] == site.key]
        if rows.empty:
            states.append(SiteState(key=site.key, name=site.name, kind=site.kind))
            continue
        values = {r["variable"]: float(r["value"]) for _, r in rows.iterrows()}
        last_seen = rows["timestamp"].max().to_pydatetime()
        states.append(SiteState(
            key=site.key, name=site.name, kind=site.kind, values=values,
            last_seen=last_seen,
            age_hours=(now - last_seen).total_seconds() / 3600,
        ))

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

    spilling = [s.display_name for s in states if s.spilling]
    if spilling:
        notes.append(f"vertiendo: {', '.join(spilling)}")

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

    if report.notes:
        lines += ["", "OBSERVACIONES"]
        lines += [f"  · {n}" for n in report.notes]
    return "\n".join(lines)
