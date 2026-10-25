"""Panel HTML de la cascada del Iguazú.

Una tarjeta por usina, de aguas arriba hacia aguas abajo, seguidas por las
estaciones del tronco. El HTML sale autocontenido: CSS en línea, sparklines
en SVG y sin JavaScript, para abrirlo desde disco o publicarlo tal cual.

`fragment=True` omite <!doctype>/<html>/<head>/<body>, para incrustarlo en
otra página o publicarlo en un visor que pone su propio esqueleto.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape

from iguacu_monitor.config import Config
from iguacu_monitor.model import LEVEL, QAFL, QTUR, QVER, STORAGE
from iguacu_monitor.report import ALERTA, ATENCION, NIVEL, NORMAL, Report, SiteState

DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
MESES = ["ene", "feb", "mar", "abr", "may", "jun",
         "jul", "ago", "sep", "oct", "nov", "dic"]

FONTS = ("https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;600"
         "&family=Barlow+Semi+Condensed:wght@600;700&display=swap")


# ------------------------------------------------------------------ formato
def fmt_int(value: float | None) -> str:
    """Entero con punto de miles, como se escribe en el Cono Sur: 8.978."""
    if value is None:
        return "—"
    return f"{value:,.0f}".replace(",", ".")


def fmt_dec(value: float | None, digits: int = 1) -> str:
    """Decimal con coma: 737,6."""
    if value is None:
        return "—"
    text = f"{value:,.{digits}f}"
    return text.replace(",", "\0").replace(".", ",").replace("\0", ".")


def fmt_trend(per_h: float | None) -> str:
    if per_h is None:
        return "—"
    sign = "+" if per_h >= 0 else "−"
    return f"{sign}{fmt_int(abs(per_h))}/h"


def fmt_travel(hours: float | None) -> str:
    if hours is None:
        return "—"
    shown = fmt_int(hours) if float(hours).is_integer() else fmt_dec(hours)
    return f"≈ {shown} h"


ACRONYMS = {"UHE", "PCH", "CGH", "AHE", "UTE", "II", "III", "IV", "PR", "SC", "FDA"}


def station_title(name: str) -> str:
    """'CGH CAVERNOSO BARRAMENTO' -> 'CGH Cavernoso Barramento'."""
    words = []
    for word in name.split():
        bare = word.strip(".,()").upper()
        words.append(word.upper() if bare in ACRONYMS else word.capitalize())
    return " ".join(words)


def join_names(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " y " + names[-1]


# -------------------------------------------------------------- sparkline
def sparkline(state: SiteState, width: int = 200, height: int = 44) -> str:
    points = state.series
    if len(points) < 2:
        return f'<div class="spark spark-empty" aria-hidden="true"></div>'

    t0, t1 = points[0][0], points[-1][0]
    span = (t1 - t0).total_seconds() or 1.0
    values = [v for _, v in points]
    lo, hi = min(values), max(values)
    pad = (hi - lo) * 0.12 or max(abs(hi) * 0.02, 1.0)
    lo, hi = lo - pad, hi + pad

    def xy(t: datetime, v: float) -> tuple[float, float]:
        x = (t - t0).total_seconds() / span * width
        y = height - (v - lo) / (hi - lo) * height
        return round(x, 2), round(y, 2)

    coords = [xy(t, v) for t, v in points]
    line = "M" + " L".join(f"{x},{y}" for x, y in coords)
    area = f"{line} L{coords[-1][0]},{height} L{coords[0][0]},{height} Z"
    ex, ey = coords[-1]
    label = (f"{state.display_name}: últimas "
             f"{round(span / 3600)} h, de {fmt_int(values[0])} a {fmt_int(values[-1])} m³/s")
    # preserveAspectRatio="none" deja que la línea ocupe todo el ancho de la
    # tarjeta; vector-effect evita que el trazo se deforme al estirarse. El
    # punto final es una línea de largo cero con extremo redondo, que sigue
    # siendo un círculo aun con escala no uniforme.
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
        f'role="img" aria-label="{escape(label)}">'
        f'<line class="grid" x1="0" y1="{height / 2}" x2="{width}" y2="{height / 2}" '
        f'vector-effect="non-scaling-stroke"/>'
        f'<path class="area" d="{area}"/>'
        f'<path class="line" d="{line}" vector-effect="non-scaling-stroke"/>'
        f'<line class="end" x1="{ex}" y1="{ey}" x2="{ex}" y2="{ey}" '
        f'vector-effect="non-scaling-stroke"/>'
        f'</svg>'
    )


# ----------------------------------------------------------------- tarjetas
CHIP = {
    NIVEL: ("chip-nivel", "Opera por nivel"),
    NORMAL: ("chip-normal", "Normal"),
    ATENCION: ("chip-atencion", "Atención"),
    ALERTA: ("chip-alerta", "Alerta"),
}
TREND_WORD = {"up": "sube", "down": "baja", "flat": "estable"}
TREND_ARROW = {"up": "▲", "down": "▼", "flat": "▶"}


def _trend_html(state: SiteState, band: float) -> str:
    direction = state.trend_direction(band)
    if state.trend_per_h is None:
        return '<div class="trend flat">—</div>'
    words = f"{TREND_WORD[direction]} {fmt_int(abs(state.trend_per_h))} m³/s por hora"
    return (f'<div class="trend {direction}" aria-label="{words}">'
            f'<span aria-hidden="true">{TREND_ARROW[direction]} {fmt_trend(state.trend_per_h)}</span></div>')


def _rows(pairs: list[tuple[str, str]]) -> str:
    return '<dl class="rows">' + "".join(
        f"<div><dt>{escape(k)}</dt><dd>{v}</dd></div>" for k, v in pairs) + "</dl>"


def _age_html(state: SiteState) -> str:
    if state.stale and state.age_hours is not None:
        return f'<p class="age">dato de hace {fmt_int(state.age_hours)} h</p>'
    return ""


def plant_card(state: SiteState, band: float) -> str:
    site = state.site
    status = state.status or "sin_dato"
    chip_class, chip_text = CHIP.get(status, ("chip-sindato", "Sin dato"))
    if status in (ATENCION, ALERTA):
        limit = site.alert if status == ALERTA else site.attention
        chip_text = f"{chip_text} ≥ {fmt_int(limit)}"
    elif status == NORMAL:
        chip_text = f"{chip_text} &lt; {fmt_int(site.attention)}"

    storage = state.values.get(STORAGE)
    fill = max(0.0, min(100.0, storage)) if storage is not None else 0.0
    tank_label = f"{fmt_int(storage)}%" if storage is not None else "—"
    tank_aria = (f"Volumen útil {fmt_int(storage)} %" if storage is not None
                 else "Volumen útil sin dato")

    level = state.values.get(LEVEL)
    rows = _rows([
        ("entra", fmt_int(state.values.get(QAFL))),
        ("vierte", fmt_int(state.values.get(QVER))),
        ("turbina", fmt_int(state.values.get(QTUR))),
        ("nivel", f"{fmt_dec(level)} m" if level is not None else "—"),
        ("viaje", fmt_travel(site.travel_h)),
    ])

    return f'''<article class="card is-{status}">
  <header class="card-h"><span class="ord" aria-label="Posición {site.order} en la cascada">{site.order}</span><h2>{escape(state.display_name)}</h2></header>
  <p class="chip {chip_class}">{chip_text}</p>
  <div class="hero">
    <div class="tank" role="img" aria-label="{tank_aria}"><div class="fill" style="height:{fill:.0f}%"></div><span>{tank_label}</span></div>
    <div class="big">
      <div class="num">{fmt_int(state.headline)}</div>
      <div class="unit">m³/s que sale</div>
      {_trend_html(state, band)}
    </div>
  </div>
  {rows}
  {_age_html(state)}
  {sparkline(state)}
</article>'''


def station_card(state: SiteState, band: float, tz: timezone, series_class: str) -> str:
    site = state.site
    peak = state.peak
    if peak:
        local = peak[0].astimezone(tz)
        peak_value, peak_when = fmt_int(peak[1]), f"{local.day} · {local.hour} h"
    else:
        peak_value = peak_when = "—"
    code = f"Tronco {site.ana_code}" if site.ana_code else "Sin código ANA"
    rows = _rows([
        ("máximo", peak_value),
        ("el", peak_when),
        ("viaje", fmt_travel(site.travel_h)),
    ])
    return f'''<article class="card is-station {series_class}">
  <header class="card-h"><h2>{escape(site.label)}</h2></header>
  <p class="chip chip-code">{escape(code)}</p>
  <div class="hero">
    <svg class="ring" viewBox="0 0 34 34" aria-hidden="true"><circle cx="17" cy="17" r="13"/><circle class="core" cx="17" cy="17" r="5"/></svg>
    <div class="big">
      <div class="num">{fmt_int(state.headline)}</div>
      <div class="unit">m³/s</div>
      {_trend_html(state, band)}
    </div>
  </div>
  {rows}
  {_age_html(state)}
  {sparkline(state)}
</article>'''


# ------------------------------------------------------ onda hacia Cataratas
def _when(t: datetime, tz: timezone) -> str:
    local = t.astimezone(tz)
    return f"{DIAS[local.weekday()]} {local:%H} h"


def _when_range(a: datetime, b: datetime | None, tz: timezone) -> str:
    """'dom 09–10 h', o 'sáb 23 h – dom 00 h' si cruza el día."""
    if b is None or abs((b - a).total_seconds()) < 1800:
        return _when(a, tz)
    lo, hi = sorted((a.astimezone(tz), b.astimezone(tz)))
    if lo.date() == hi.date():
        return f"{DIAS[lo.weekday()]} {lo:%H}–{hi:%H} h"
    return f"{_when(lo, tz)} – {_when(hi, tz)}"


def wave_section(report: Report, cfg: Config, tz: timezone) -> str:
    from iguacu_monitor.model import QDEF

    w = report.wave
    if not w or not w.arrivals:
        return ""
    target = cfg.site((cfg.report.get("panel_stations") or [])[-1]).label
    up_label = next((s.site.label for s in report.sites if s.display_name == w.last_leg and s.site), w.last_leg)
    est = (f", {fmt_int(w.lag_est_h)} h según las últimas 48 h (r {fmt_dec(w.lag_r, 2)})"
           if w.lag_est_h is not None else "")
    lede = (f"Lo que ya pasó aguas arriba, trasladado por el tiempo de viaje. {up_label}→{target}: "
            f"{fmt_int(w.lag_cfg_h)} h configurado{est}. No incluye lluvia futura ni el aporte lateral "
            f"entre puntos.")
    steps = []
    tgt_state = next((s for s in report.sites if s.display_name == w.target), None)
    if w.target_q is not None and tgt_state and tgt_state.last_seen:
        steps.append(f'<li class="now"><span class="k">Ahora en {escape(target)}</span>'
                     f'<span class="v">{fmt_int(w.target_q)}</span>'
                     f'<span class="t">{_when(tgt_state.last_seen, tz)}</span></li>')
    if w.rising:
        eta = w.front_seen + timedelta(hours=w.front_lag_h)
        alt = (w.front_seen + timedelta(hours=w.lag_est_h)) if w.lag_est_h is not None else None
        steps.append(f'<li><span class="k">Empieza a subir</span><span class="v">+{fmt_int(w.rise_q)}</span>'
                     f'<span class="t">{_when_range(eta, alt, tz)}</span>'
                     f'<span class="src">lo que subió {escape(up_label)} desde el {_when(w.front_seen, tz)}</span></li>')
    for a in w.arrivals:
        label = next((s.site.label for s in report.sites if s.display_name == a.name and s.site), a.name)
        what = f"Llega lo que sale de {label}" if a.variable == QDEF else f"Llega lo que pasa por {label}"
        steps.append(f'<li><span class="k">{escape(what)}</span><span class="v">{fmt_int(a.q)}</span>'
                     f'<span class="t">{_when_range(a.eta, a.eta_alt, tz)}</span>'
                     f'<span class="src">pasó el {_when(a.seen, tz)} · +{fmt_int(a.lag_h)} h</span></li>')
    return f'''<section class="wave" aria-label="Onda hacia {escape(target)}">
  <div><h2>Onda hacia {escape(target)}</h2><p class="lede">{escape(lede)}</p></div>
  <ol class="steps">{"".join(steps)}</ol>
</section>'''


# --------------------------------------------------------- lluvia por subcuenca
# Rampa azul validada (pasos 700 -> 100). En fondo oscuro el ancla se invierte:
# lo seco queda cerca del fondo y lo lluvioso brilla.
RAIN_BREAKS = (10, 25, 50, 75, 100, 150)              # mm, límites de clase
RAIN_RAMP = ("#0d366b", "#184f95", "#256abf", "#3987e5", "#6da7ec", "#9ec5f4", "#cde2fb")


def rain_color(mm: float | None) -> str:
    if mm is None:
        return "var(--panel-hi)"
    for i, edge in enumerate(RAIN_BREAKS):
        if mm < edge:
            return RAIN_RAMP[i]
    return RAIN_RAMP[-1]


def _project(cfg_bounds, width: float = 1000.0, pad: float = 14.0):
    """Equirectangular con escala cos(lat) centrada en la cuenca."""
    import math

    minx, miny, maxx, maxy = cfg_bounds
    k = math.cos(math.radians((miny + maxy) / 2))
    scale = (width - 2 * pad) / ((maxx - minx) * k)
    height = (maxy - miny) * scale + 2 * pad

    def xy(lon: float, lat: float) -> tuple[float, float]:
        return (round(pad + (lon - minx) * k * scale, 1), round(pad + (maxy - lat) * scale, 1))
    return xy, round(height)


def _path(geom, xy) -> str:
    polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
    parts = []
    for poly in polys:
        for ring in [poly.exterior, *poly.interiors]:
            pts = [xy(x, y) for x, y in ring.coords]
            parts.append("M" + " L".join(f"{x},{y}" for x, y in pts) + " Z")
    return " ".join(parts)


def rain_section(report: Report, cfg: Config, window: int = 24) -> str:
    from iguacu_monitor import subbasins

    if not report.subbasins or not cfg.subbasins_path:
        return ""
    subs = {s.id: s for s in subbasins.load(cfg.subbasins_path)}
    if not subs:
        return ""
    contour = subbasins.outline(list(subs.values()))
    xy, height = _project(contour.bounds)

    shapes, labels = [], []
    for sb in report.subbasins:
        geom = subs[sb.id].geom.simplify(0.008, preserve_topology=True)
        mm = sb.gauge_mm.get(window)
        bits = [f"{w} h {fmt_dec(sb.gauge_mm[w])} mm" for w in sorted(sb.gauge_mm)]
        tip = (f"{sb.order}. {sb.name} · área incremental {fmt_int(sb.area_km2)} km² · "
               + " · ".join(bits) + f" · {sb.n_gauges} pluviómetros con dato")
        shapes.append(f'<path class="sb" d="{_path(geom, xy)}" fill="{rain_color(mm)}" '
                      f'fill-rule="evenodd"><title>{escape(tip)}</title></path>')
        pt = subs[sb.id].geom.representative_point()
        lx, ly = xy(pt.x, pt.y)
        labels.append(f'<text class="ord-label" x="{lx}" y="{ly + 8}">{sb.order}</text>')

    gauges = [f'<circle class="gauge" cx="{x}" cy="{y}" r="3.2"/>'
              for x, y in (xy(g.lon, g.lat) for g in cfg.gauges
                           if g.lon is not None and contour.buffer(0.02).contains(_point(g.lon, g.lat)))]
    outlets = []
    for key in cfg.subbasins_opts.get("outlets") or []:
        site = cfg.site(key)
        if site.lon is None:
            continue
        x, y = xy(site.lon, site.lat)
        outlets.append(f'<rect class="outlet" x="{x - 6}" y="{y - 6}" width="12" height="12" '
                       f'transform="rotate(45 {x} {y})"><title>{escape(site.label)}</title></rect>')

    swatches = "".join(f'<span class="sw" style="background:{c}"></span>' for c in RAIN_RAMP)
    ticks = "".join(f"<span>{t}</span>" for t in ("0", *map(str, RAIN_BREAKS)))
    legend = f'''<ul class="legend" aria-label="Escala de lluvia en {window} h, en milímetros">
  <li><div class="ramp">{swatches}</div><div class="ticks">{ticks}</div></li>
  <li>mm en {window} h</li>
  <li class="sym"><svg viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="3.5" fill="currentColor"/></svg>pluviómetro</li>
  <li class="sym"><svg viewBox="0 0 12 12" aria-hidden="true"><rect x="2.5" y="2.5" width="7" height="7" transform="rotate(45 6 6)" fill="currentColor"/></svg>punto de control</li>
</ul>'''

    windows = sorted({w for sb in report.subbasins for w in sb.gauge_mm})
    head = "".join(f"<th>{w} h</th>" for w in windows)
    rows = []
    without = [sb.name for sb in report.subbasins if sb.n_gauges == 0]
    for sb in report.subbasins:
        cells = []
        for w in windows:
            v = sb.gauge_mm.get(w)
            cls = ' class="hl"' if w == window else ""
            cells.append(f"<td{cls}>{fmt_dec(v) if v is not None else '—'}</td>")
        m1 = sb.merge_mm.get(1)
        n_cls = ' class="dim"' if sb.n_gauges == 0 else ""
        rows.append(
            f"<tr><td>{sb.order}</td>"
            f'<td><span class="chip-sw" style="background:{rain_color(sb.gauge_mm.get(window))}"></span>'
            f"{escape(sb.name)}</td>"
            f"<td class=\"dim\">{fmt_int(sb.area_km2)}</td><td{n_cls}>{sb.n_gauges}</td>"
            + "".join(cells)
            + f"<td class=\"dim\">{fmt_dec(m1) if m1 is not None else '—'}</td></tr>")
    note = (f"{escape(join_names(without))} no {'tiene' if len(without) == 1 else 'tienen'} "
            f"pluviómetros dentro: su valor sale de interpolar los vecinos. " if without else "")
    who = cfg.report.get("processed_by")
    credit = f'\n    <p class="credit">Procesado por: {escape(who)}</p>' if who else ""
    return f'''<section class="rain" aria-label="Lluvia por subcuenca">
  <div class="map-card">
    <div><h2>Lluvia por subcuenca · {window} h</h2>
      <p class="lede">Áreas incrementales de la cascada, de la cabecera a Cataratas. Pluviómetros ANA interpolados (IDW).</p></div>
    <svg class="map" viewBox="0 0 1000 {height}" role="img" aria-label="Mapa de lluvia en {window} horas por subcuenca">
      {"".join(shapes)}{"".join(gauges)}{"".join(outlets)}{"".join(labels)}
    </svg>
    {legend}
  </div>
  <div class="tbl-card">
    <div class="tbl-wrap"><table class="sbt">
      <thead><tr><th>#</th><th>Área incremental</th><th>km²</th><th>pluv.</th>{head}<th>MERGE 1 d</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table></div>
    <p class="tbl-note">Valores en mm. {note}"pluv." cuenta los pluviómetros con dato en 24 h. MERGE: lámina diaria satelital-pluviométrica de INPE, de 12 a 12 UTC, con rezago. Subcuencas delimitadas con HydroSHEDS y validadas contra las áreas de drenaje de ANA (error ≤ 0,2 %).</p>{credit}
  </div>
</section>'''


def _point(lon: float, lat: float):
    from shapely.geometry import Point
    return Point(lon, lat)


# -------------------------------------------------------------------- página
CSS = """
/* Sala de control: azul profundo de agua; el estado (ámbar, rojo) es el
   único color fuerte. Cascada en fila: 8 tarjetas, luego 4 + 4, luego 2. */
:root {
  --bg: #0a1524;
  --panel: #11213a;
  --panel-hi: #172b48;
  --line: #24395a;
  --fg: #e9eff7;
  --muted: #93a7c1;
  --faint: #5f7594;
  --water: #42a7ee;
  --water-deep: #1c6aae;
  --warn: #f3b43a;
  --warn-ink: #2a1d00;
  --warn-bg: #352a10;
  --warn-line: #9c741f;
  --crit: #f0525c;
  --crit-ink: #ffffff;
  --crit-bg: #3a151b;
  --crit-line: #a8343e;
  --cap: #eef3f9;
  --cat: #ff7385;
  --shadow: rgb(0 0 0 / 0.55);
  --font-ui: "Barlow", system-ui, "Segoe UI", Roboto, sans-serif;
  --font-num: "Barlow Semi Condensed", "Barlow", system-ui, "Segoe UI", Roboto, sans-serif;
  color-scheme: dark;
}
body { margin: 0; background: var(--bg); color: var(--fg); font-family: var(--font-ui); font-size: 15px; }
.wrap {
  max-width: 1880px; margin: 0 auto; display: grid; gap: 14px;
  padding-left: max(16px, env(safe-area-inset-left, 0px));
  padding-right: max(16px, env(safe-area-inset-right, 0px));
  padding-block: 20px 28px;
}
.top { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: end; gap: 8px 24px; }
.top h1 { margin: 0; font-family: var(--font-num); font-weight: 700; font-size: 1.05rem;
  letter-spacing: 0.09em; text-transform: uppercase; text-wrap: balance; }
.top .sub { margin: 2px 0 0; color: var(--muted); font-size: 0.9rem; }
.clock { text-align: right; font-variant-numeric: tabular-nums; }
.clock .t { display: block; font-family: var(--font-num); font-weight: 600; font-size: 2.3rem; line-height: 1; }
.clock .d { color: var(--muted); font-size: 0.88rem; }
.demo { margin: 0; border: 1px dashed var(--faint); border-radius: 8px; padding: 8px 12px;
  color: var(--muted); font-size: 0.88rem; background: var(--panel); }
.demo strong { color: var(--fg); }
.summary { display: flex; flex-wrap: wrap; gap: 8px; margin: 0; padding: 0; list-style: none; }
.pill { border: 1px solid var(--line); border-radius: 999px; padding: 4px 12px; font-size: 0.9rem;
  background: var(--panel); }
.pill .k { color: var(--muted); margin-right: 6px; }
.pill.crit { border-color: var(--crit-line); background: var(--crit-bg); }
.pill.crit .k { color: var(--crit); }
.pill.warn { border-color: var(--warn-line); background: var(--warn-bg); }
.pill.warn .k { color: var(--warn); }
.rule { margin: 0; color: var(--muted); font-size: 0.9rem; line-height: 1.45; }
.rule b { color: var(--fg); font-weight: 600; }
.rule .w { color: var(--warn); font-weight: 600; }
.rule .c { color: var(--crit); font-weight: 600; }

.cascade { display: grid; gap: 12px; grid-template-columns: 1fr; }
.card { position: relative; min-width: 0; display: grid; gap: 8px; align-content: start; container-type: inline-size;
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px; }
.card.is-atencion { background: var(--warn-bg); border-color: var(--warn-line); }
.card.is-alerta { background: var(--crit-bg); border-color: var(--crit-line); }
.card::after { content: "›"; display: none; position: absolute; right: -11px; top: 42%;
  font-family: var(--font-num); font-size: 1.5rem; line-height: 1; color: var(--faint); pointer-events: none; }
.card-h { display: flex; align-items: center; gap: 8px; min-width: 0; }
.card-h h2 { margin: 0; font-family: var(--font-num); font-weight: 700; font-size: 1.12rem; line-height: 1.15;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ord { flex: none; display: grid; place-items: center; width: 1.45rem; height: 1.45rem; border-radius: 50%;
  background: var(--fg); color: var(--bg); font-family: var(--font-num); font-weight: 700; font-size: 0.85rem; }
.chip { justify-self: start; margin: 0; padding: 2px 8px; border-radius: 4px; font-family: var(--font-num);
  font-weight: 700; font-size: 0.78rem; letter-spacing: 0.07em; text-transform: uppercase; white-space: nowrap; }
.chip-nivel, .chip-code { background: var(--panel-hi); border: 1px solid var(--line); color: var(--fg); }
.chip-code { color: var(--muted); }
.chip-normal, .chip-sindato { border: 1px solid var(--line); color: var(--muted); }
.chip-atencion { background: var(--warn); color: var(--warn-ink); }
.chip-alerta { background: var(--crit); color: var(--crit-ink); }

.hero { display: grid; grid-template-columns: 34px minmax(0, 1fr); gap: 10px; align-items: center; }
.tank { position: relative; width: 34px; height: 66px; border: 2px solid var(--muted); border-radius: 5px 5px 8px 8px;
  overflow: hidden; background: var(--bg); box-sizing: border-box; }
.tank .fill { position: absolute; inset: auto 0 0 0; background: linear-gradient(var(--water), var(--water-deep)); }
.tank span { position: absolute; inset: 0; display: grid; place-items: center; font-family: var(--font-num);
  font-weight: 700; font-size: 0.72rem; color: var(--fg); text-shadow: 0 1px 2px var(--shadow); }
.ring { width: 34px; height: 34px; }
.ring circle { fill: none; stroke: currentColor; stroke-width: 3; }
.ring .core { fill: currentColor; stroke: none; }
.is-station.cap .ring, .is-station.cap .spark { color: var(--cap); }
.is-station.cat .ring, .is-station.cat .spark { color: var(--cat); }
.big { min-width: 0; }
/* Relativo al ancho de la tarjeta: "15.616" tiene que entrar junto al tanque
   tanto en la fila de 8 como en las 2 columnas del celular. */
.num { font-family: var(--font-num); font-weight: 700; font-size: clamp(1.7rem, 1vw + 1.1rem, 2.6rem);
  font-size: clamp(1.6rem, 17cqi, 2.8rem); line-height: 1; font-variant-numeric: tabular-nums; white-space: nowrap; }
.unit { color: var(--muted); font-size: 0.82rem; margin-top: 2px; }
.trend { font-family: var(--font-num); font-weight: 700; font-size: 1.05rem; font-variant-numeric: tabular-nums; margin-top: 2px; }
.trend.up { color: var(--warn); }
.trend.down { color: var(--water); }
.trend.flat { color: var(--muted); }
.rows { margin: 0; display: grid; gap: 3px; }
.rows div { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.rows dt { color: var(--muted); font-size: 0.9rem; }
.rows dd { margin: 0; font-family: var(--font-num); font-weight: 600; font-size: 1.06rem; font-variant-numeric: tabular-nums; white-space: nowrap; }
.age { margin: 0; color: var(--warn); font-size: 0.82rem; }
.spark { display: block; width: 100%; height: 42px; color: var(--water); }
.spark .grid { stroke: var(--line); stroke-width: 1; stroke-dasharray: 3 3; }
.spark .area { fill: currentColor; opacity: 0.16; }
.spark .line { fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linejoin: round; }
.spark .end { stroke: currentColor; stroke-width: 6; stroke-linecap: round; }
.spark-empty { border-top: 1px dashed var(--line); height: 0; margin-top: 21px; }

/* Lluvia por subcuenca: rampa azul de una sola tinta, ancla invertida para
   el fondo oscuro (lo seco se funde con el panel, lo lluvioso brilla). */
.rain { display: grid; gap: 12px; grid-template-columns: 1fr; align-items: start; }
.rain h2 { margin: 0; font-family: var(--font-num); font-weight: 700; font-size: 1.05rem;
  letter-spacing: 0.09em; text-transform: uppercase; }
.rain .lede { margin: 2px 0 0; color: var(--muted); font-size: 0.88rem; }
.map-card, .tbl-card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 12px; min-width: 0; display: grid; gap: 10px; }
.map { display: block; width: 100%; height: auto; }
.map .sb { stroke: var(--bg); stroke-width: 2; stroke-linejoin: round; }
.map .sb:hover { stroke: var(--fg); }
.map .outlet { fill: var(--fg); stroke: var(--bg); stroke-width: 2; }
.map .gauge { fill: var(--fg); fill-opacity: 0.85; stroke: var(--bg); stroke-width: 1.5; }
.map .ord-label { font-family: var(--font-num); font-weight: 700; font-size: 24px; fill: var(--bg);
  paint-order: stroke; stroke: var(--fg); stroke-width: 5px; stroke-linejoin: round; text-anchor: middle; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 14px; align-items: center; margin: 0; padding: 0;
  list-style: none; color: var(--muted); font-size: 0.82rem; }
.legend .ramp { display: flex; gap: 2px; align-items: center; }
.legend .sw { width: 26px; height: 12px; border-radius: 2px; }
.legend .ticks { display: flex; gap: 2px; font-variant-numeric: tabular-nums; }
.legend .ticks span { width: 26px; text-align: left; font-size: 0.72rem; }
.legend .sym { display: inline-flex; align-items: center; gap: 6px; }
.legend svg { width: 12px; height: 12px; }
.tbl-wrap { overflow-x: auto; }
table.sbt { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; font-size: 0.9rem; }
.sbt th { text-align: right; color: var(--muted); font-weight: 500; font-size: 0.8rem; padding: 4px 6px;
  border-bottom: 1px solid var(--line); white-space: nowrap; }
.sbt th:first-child, .sbt td:first-child, .sbt th:nth-child(2), .sbt td:nth-child(2) { text-align: left; }
.sbt td { text-align: right; padding: 5px 6px; border-bottom: 1px solid var(--line); white-space: nowrap;
  font-family: var(--font-num); font-weight: 600; }
.sbt td:nth-child(2) { font-family: var(--font-ui); font-weight: 500; }
.sbt td.hl { color: var(--fg); }
.sbt td.dim, .sbt td .dim { color: var(--muted); font-weight: 500; }
.sbt .chip-sw { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; vertical-align: -1px; }
.sbt tr:last-child td { border-bottom: 0; }
.tbl-note { margin: 0; color: var(--muted); font-size: 0.8rem; }
.credit { margin: 0; padding-top: 10px; border-top: 1px solid var(--line); color: var(--muted); font-size: 0.8rem; font-weight: 500; }
@media (min-width: 1100px) { .rain { grid-template-columns: minmax(0, 1.55fr) minmax(0, 1fr); } }

/* Onda hacia Cataratas: una secuencia en el tiempo, de lo que hay ahora a lo
   que ya viene en camino. */
.wave { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px;
  display: grid; gap: 10px; }
.wave h2 { margin: 0; font-family: var(--font-num); font-weight: 700; font-size: 1.05rem;
  letter-spacing: 0.09em; text-transform: uppercase; }
.wave .lede { margin: 2px 0 0; color: var(--muted); font-size: 0.88rem; line-height: 1.4; }
.steps { margin: 0; padding: 0; list-style: none; display: grid; gap: 10px;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
.steps li { position: relative; min-width: 0; display: grid; gap: 2px; align-content: start;
  background: var(--panel-hi); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; }
.steps li.now { background: var(--bg); }
.steps .k { color: var(--muted); font-size: 0.8rem; }
.steps .v { font-family: var(--font-num); font-weight: 700; font-size: 1.7rem; line-height: 1.05;
  font-variant-numeric: tabular-nums; }
.steps .t { font-family: var(--font-num); font-weight: 700; font-size: 1.05rem; color: var(--warn); }
.steps .now .t { color: var(--muted); }
.steps .src { color: var(--muted); font-size: 0.78rem; }

.foot { display: grid; gap: 6px; color: var(--muted); font-size: 0.85rem; }
.foot ul { margin: 0; padding-left: 1.1em; display: grid; gap: 2px; }
.foot p { margin: 0; }

@media (min-width: 380px) {
  .cascade { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .card::after { display: block; }
  .card:nth-child(2n)::after { display: none; }
}
@media (min-width: 860px) {
  .cascade { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .card:nth-child(2n)::after { display: block; }
  .card:nth-child(4n)::after { display: none; }
}
@media (min-width: 1680px) {
  .cascade { grid-template-columns: repeat(8, minmax(0, 1fr)); }
  .card:nth-child(4n)::after { display: block; }
  .card:nth-child(8n)::after { display: none; }
}
.card:last-child::after { display: none !important; }
"""


def _rule_line(cfg: Config) -> str:
    groups: dict[tuple[float, float], list[str]] = {}
    by_level: list[str] = []
    for plant in cfg.plants:
        if plant.operation == "nivel":
            by_level.append(plant.label)
        else:
            groups.setdefault((plant.attention, plant.alert), []).append(plant.label)
    parts = [
        f'{escape(join_names(names))} <span class="w">atención ≥ {fmt_int(att)}</span>'
        f' · <span class="c">alerta ≥ {fmt_int(alert)}</span>'
        for (att, alert), names in groups.items()
    ]
    text = " | ".join(parts) + (" m³/s" if parts else "")
    if by_level:
        text += f" | {escape(join_names(by_level))} operan por nivel"
    source = cfg.report.get("threshold_source")
    origin = f" ({escape(source)})" if source else ""
    return f'<p class="rule">Situación por el caudal que <b>entra</b>{origin}: {text}</p>'


def _summary(report: Report, tz: timezone | None = None) -> str:
    pills: list[str] = []
    w = report.wave
    if tz and w and w.arrivals and w.target_q is not None:
        big = max(w.arrivals, key=lambda a: a.q)
        if big.q > w.target_q * 1.05:
            pills.append(f'<li class="pill warn"><span class="k">Onda a {escape(w.target.replace("Hotel ", ""))}</span>'
                         f'≈ {fmt_int(big.q)} m³/s · {_when_range(big.eta, big.eta_alt, tz)}</li>')
    alert = report.by_status(ALERTA)
    warn = report.by_status(ATENCION)
    if alert:
        pills.append(f'<li class="pill crit"><span class="k">En alerta</span>'
                     f'{escape(join_names([s.site.label for s in alert]))}</li>')
    if warn:
        pills.append(f'<li class="pill warn"><span class="k">En atención</span>'
                     f'{escape(join_names([s.site.label for s in warn]))}</li>')
    with_data = [s for s in report.plants if s.values]
    if with_data:
        pills.append(f'<li class="pill"><span class="k">Vierten</span>'
                     f'{len(report.spilling)} de {len(with_data)} usinas</li>')
    if report.gauges:
        g = report.gauges.get(24) or report.gauges[max(report.gauges)]
        short = [f"{w} h {fmt_dec(report.gauges[w].median)}" for w in (1, 6) if w in report.gauges]
        pills.append(
            f'<li class="pill"><span class="k">Lluvia medida {g.window_h} h</span>'
            f'mediana {fmt_dec(g.median)} mm · máx {fmt_dec(g.max_value)} mm '
            f'({escape(station_title(g.max_name))}) · {" · ".join(short + [f"{g.n} pluviómetros"])}</li>')
    if report.precip:
        bits = [f"{w} d {fmt_dec(report.precip[w])} mm"
                for w in (1, 3, 7) if w in report.precip]
        pills.append(f'<li class="pill"><span class="k">MERGE media areal</span>'
                     f'{" · ".join(bits)}</li>')
    return f'<ul class="summary">{"".join(pills)}</ul>' if pills else ""


def render(report: Report, cfg: Config, *, note: str | None = None,
           fragment: bool = False) -> str:
    opts = cfg.report
    band = float(opts.get("trend_flat_band", 15))
    tz = timezone(timedelta(hours=float(opts.get("display_utc_offset_h", -3))))

    # El reloj muestra el instante del dato más reciente, no el de la
    # consulta: es lo que el operador necesita saber.
    stamps = [s.last_seen for s in report.sites if s.last_seen]
    shown = max(stamps) if stamps else report.generated
    local = shown.astimezone(tz)
    clock = f"{local:%H:%M}"
    date_line = f"{DIAS[local.weekday()]} {local.day} {MESES[local.month - 1]} {local.year} · hora local"

    cards = [plant_card(s, band) for s in report.plants]
    wanted = opts.get("panel_stations") or [s.key for s in report.stations]
    station_by_key = {s.key: s for s in report.stations}
    for i, key in enumerate(wanted):
        if key in station_by_key:
            series_class = "cap" if i == 0 else "cat"
            cards.append(station_card(station_by_key[key], band, tz, series_class))

    notes = "".join(f"<li>{escape(n)}</li>" for n in report.notes)
    demo = (f'<p class="demo"><strong>Datos de ejemplo.</strong> {escape(note)}</p>'
            if note else "")

    head = f'''<title>Cascada del Iguazú</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONTS}">
<style>{CSS}</style>'''

    body = f'''<main class="wrap">
  <header class="top">
    <div>
      <h1>Cuenca del {escape(cfg.basin.name.replace("Rio ", "").replace("Iguaçu", "Iguazú"))}</h1>
      <p class="sub">Regulación de la cascada y caudal en el tronco</p>
    </div>
    <div class="clock"><span class="t">{clock}</span><span class="d">{date_line}</span></div>
  </header>
  {demo}
  {_summary(report, tz)}
  {_rule_line(cfg)}
  <section class="cascade" aria-label="Cascada del Iguazú, de aguas arriba hacia aguas abajo">
{chr(10).join(cards)}
  </section>
  {wave_section(report, cfg, tz)}
  {rain_section(report, cfg)}
  <footer class="foot">
    {f"<ul>{notes}</ul>" if notes else ""}
    <p>Fuentes: ONS (usinas, horario), ANA (caudal y pluviómetros), INPE MERGE (lluvia diaria), HydroSHEDS (subcuencas; Lehner et al., 2008) · la lluvia medida de la cabecera es la mediana de los pluviómetros, no una media areal · generado {report.generated:%Y-%m-%d %H:%M} UTC</p>
  </footer>
</main>'''

    if fragment:
        return f"{head}\n{body}\n"
    return (f'<!doctype html>\n<html lang="es">\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            f'{head}\n</head>\n<body>\n{body}\n</body>\n</html>\n')
