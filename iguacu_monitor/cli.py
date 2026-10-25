"""Interfaz de línea de comandos del monitor."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from iguacu_monitor import config, demo, panel, report, store
from iguacu_monitor.sources.ana import ANA
from iguacu_monitor.sources.cemaden import Cemaden
from iguacu_monitor.sources.copel_mhb import CopelMHB
from iguacu_monitor.sources.inpe_merge import InpeMerge
from iguacu_monitor.sources.ons import ONS

SOURCES = {
    "copel_mhb": CopelMHB,
    "ana": ANA,
    "cemaden": Cemaden,
    "inpe_merge": InpeMerge,
    "ons": ONS,
}


def _build(cfg: config.Config, only: str | None):
    chosen = [only] if only else list(SOURCES)
    out = []
    for name in chosen:
        if name not in SOURCES:
            raise SystemExit(f"fuente desconocida: {name}. Opciones: {', '.join(SOURCES)}")
        src = SOURCES[name](cfg)
        if src.enabled or only:
            out.append(src)
    return out


def cmd_probe(args) -> int:
    cfg = config.load(args.config)
    failures = 0
    for src in _build(cfg, args.source):
        result = src.probe()
        print(result.render())
        print()
        if not result.ok:
            failures += 1
    if failures:
        print(f"{failures} fuente(s) con problemas.", file=sys.stderr)
    return 1 if failures else 0


def cmd_discover(args) -> int:
    cfg = config.load(args.config)
    found = ANA(cfg).discover(args.name)
    if not found:
        print(f"sin resultados para {args.name!r}")
        return 1
    print(f"{len(found)} estación(es) para {args.name!r}:\n")
    print(f"{'código':>10}  {'nombre':<34}{'río':<22}{'oper':<8}telem")
    for row in found:
        print(f"{row['codigo']:>10}  {row['nome'][:33]:<34}"
              f"{row['rio'][:21]:<22}{row['operadora'][:7]:<8}{row['telemetrica']}")
    print("\nCopiá el código en config/iguacu.yml bajo la estación correspondiente.")
    return 0


def cmd_fetch(args) -> int:
    cfg = config.load(args.config)
    end = date.today()
    start = end - timedelta(days=args.days)
    total, added = 0, 0
    for src in _build(cfg, args.source):
        try:
            obs = src.fetch(start, end)
        except Exception as exc:  # noqa: BLE001 - una fuente caída no voltea el resto
            print(f"[FALLA] {src.name}: {exc}", file=sys.stderr)
            continue
        total += len(obs)
        added += store.save(obs, cfg.store_path)
        print(f"[OK] {src.name}: {len(obs)} observaciones")
    print(f"\n{total} leídas, {added} nuevas -> {cfg.store_path}")
    return 0


def cmd_report(args) -> int:
    cfg = config.load(args.config)
    frame = store.load(cfg.store_path)
    print(report.render_text(report.build(frame, cfg)))
    return 0


def cmd_pluviometros(args) -> int:
    import yaml
    from datetime import datetime, timezone

    cfg = config.load(args.config)
    if args.red == "cemaden":
        return _pluviometros_cemaden(cfg, args.dry_run)
    ana = ANA(cfg)
    if not ana.uses_hws:
        print("hace falta la API nueva de ANA: definí ANA_HWS_USER y ANA_HWS_PASS", file=sys.stderr)
        return 1
    if cfg.gauges_path is None:
        print("falta rain_gauges.file en la configuración", file=sys.stderr)
        return 1
    gauges, notes = ana.discover_gauges()
    for note in notes:
        print(f"· {note}")
    by_operator: dict[str, int] = {}
    for g in gauges:
        by_operator[g["operator"]] = by_operator.get(g["operator"], 0) + 1
    print("· por operadora: " + ", ".join(f"{k} {v}" for k, v in sorted(by_operator.items(), key=lambda kv: -kv[1])))
    if args.dry_run:
        return 0
    header = (f"# Pluviómetros telemétricos de ANA en la cuenca del Iguazú.\n"
              f"# Generado por `iguacu-monitor pluviometros` el "
              f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. Se puede editar a mano.\n")
    body = yaml.safe_dump({"gauges": gauges}, allow_unicode=True, sort_keys=False)
    cfg.gauges_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.gauges_path.write_text(header + body, encoding="utf-8")
    print(f"{len(gauges)} pluviómetros escritos en {cfg.gauges_path}")
    return 0


def _pluviometros_cemaden(cfg: config.Config, dry_run: bool) -> int:
    """Catálogo de CEMADEN: estaciones a menos de ~10 km del contorno, las
    mismas que la interpolación usa para no subestimar los bordes."""
    import yaml
    from datetime import datetime, timezone

    from iguacu_monitor import subbasins

    target = cfg.gauges_opts.get("cemaden_file")
    if not target or not cfg.subbasins_path:
        print("faltan rain_gauges.cemaden_file y la capa de subcuencas en la configuración", file=sys.stderr)
        return 1
    area = subbasins.outline(subbasins.load(cfg.subbasins_path)).buffer(0.1)
    gauges = Cemaden(cfg).discover(area)
    print(f"· {len(gauges)} estaciones de CEMADEN a menos de ~10 km de la cuenca")
    if dry_run:
        return 0
    header = (f"# Pluviómetros automáticos de CEMADEN en la cuenca del Iguazú (acumulado de 24 h).\n"
              f"# Generado por `iguacu-monitor pluviometros --red cemaden` el "
              f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. Se puede editar a mano.\n")
    body = yaml.safe_dump({"gauges": gauges}, allow_unicode=True, sort_keys=False)
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + body, encoding="utf-8")
    print(f"{len(gauges)} pluviómetros escritos en {path}")
    return 0


def cmd_subcuencas(args) -> int:
    """Delimita las áreas incrementales y escribe el GeoJSON versionado."""
    import io
    import json
    import tempfile
    import zipfile
    from pathlib import Path

    from iguacu_monitor import delineate, http

    cfg = config.load(args.config)
    opts = cfg.subbasins_opts
    if not opts.get("file") or not opts.get("outlets"):
        print("falta la sección subbasins (file, outlets) en la configuración", file=sys.stderr)
        return 1
    outlets = []
    for key in opts["outlets"]:
        site = cfg.site(key)
        if site.lat is None or site.lon is None:
            print(f"{key}: faltan lat/lon en la configuración", file=sys.stderr)
            return 1
        outlets.append(delineate.Outlet(key, site.label, site.lon, site.lat, site.drainage_km2))

    work = Path(args.workdir or tempfile.mkdtemp(prefix="hydrosheds_"))
    work.mkdir(parents=True, exist_ok=True)
    tif = work / "hyd_sa_dir_15s.tif"
    if not tif.exists():
        print("bajando direcciones de flujo de HydroSHEDS…")
        resp = http.get(opts.get("fdir_url", delineate.HYDROSHEDS_DIR_15S), timeout=900)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            member = next(n for n in zf.namelist() if n.lower().endswith(".tif"))
            tif.write_bytes(zf.read(member))
    bbox = tuple(opts.get("dem_bbox", (-55.0, -27.0, -48.0, -24.0)))
    crop = delineate.crop_raster(tif, bbox, work / "dir_crop.tif")
    labels, transform, summary = delineate.delineate_fdir(
        crop, outlets, float(opts.get("snap_min_frac", 0.5)))

    worst = 0.0
    print(f"\n{'punto':20}{'drenaje':>10}{'ANA':>10}{'dif':>8}{'incremental':>13}{'ajuste':>9}")
    for s in summary:
        ref = s["area_ref_km2"]
        dif = s["area_km2"] / ref - 1 if ref else float("nan")
        worst = max(worst, abs(dif)) if ref else worst
        print(f"{s['key']:20}{s['area_km2']:>10,.0f}{(ref or 0):>10,.0f}{dif * 100:>+7.1f}%"
              f"{s['incremental_km2']:>13,.0f}{s['snap_km']:>7.1f}km")
    limit = float(opts.get("max_area_error", 0.02))
    if worst > limit:
        print(f"\nla validación no cierra: error máximo {worst:.1%} > {limit:.0%}; no se escribe la capa",
              file=sys.stderr)
        return 1

    gj = delineate.to_geojson(labels, transform, outlets, summary, tolerance_deg=0.005)
    gj["metadata"] = {
        "source": "HydroSHEDS v1, direcciones de flujo 15\" (Lehner et al., 2008)",
        "method": "áreas incrementales por punto de control; ajuste al cauce principal más cercano",
        "max_area_error_vs_ana": round(worst, 4),
    }
    out = cfg.subbasins_path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(gj, ensure_ascii=False), encoding="utf-8")
    print(f"\n{len(gj['features'])} subcuencas escritas en {out} (error máximo contra ANA {worst:.2%})")
    return 0


def cmd_html(args) -> int:
    import pandas as pd

    cfg = config.load(args.config)
    if args.demo:
        frame = pd.DataFrame([o.as_row() for o in demo.example_observations()])
        rep, note = report.build(frame, cfg, now=demo.SNAPSHOT), demo.NOTE
    else:
        rep, note = report.build(store.load(cfg.store_path), cfg), None
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(panel.render(rep, cfg, note=note, fragment=args.fragment), encoding="utf-8")
    print(f"panel escrito en {out}")
    return 0


def cmd_lluvia(args) -> int:
    """Salida independiente con la lluvia por subcuenca."""
    cfg = config.load(args.config)
    windows = [int(w) for w in cfg.report.get("subbasin_windows_h", (12, 24, 36, 48, 72))]
    if args.ventana not in windows:
        print(f"la ventana debe ser una de {windows} (subbasin_windows_h)", file=sys.stderr)
        return 1
    rep = report.build(store.load(cfg.store_path), cfg)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(panel.render_rain(rep, cfg, window=args.ventana, fragment=args.fragment), encoding="utf-8")
    print(f"lluvia por subcuenca ({args.ventana} h) escrita en {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="iguacu-monitor",
        description="Monitor de regulación y precipitación — cuenca del río Iguazú",
    )
    parser.add_argument("--config", default=str(config.DEFAULT_CONFIG),
                        help="ruta del YAML de configuración")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("probe", help="diagnosticar qué devuelve cada fuente")
    p.add_argument("--source", help="probar sólo esta fuente")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("discover", help="buscar el código ANA de una estación por nombre")
    p.add_argument("name", help='fragmento del nombre, p.ej. "Capanema"')
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("fetch", help="descargar y almacenar observaciones")
    p.add_argument("--days", type=int, default=7, help="días hacia atrás (defecto: 7)")
    p.add_argument("--source", help="descargar sólo de esta fuente")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("report", help="estado actual de la cuenca")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("pluviometros", help="armar la lista de pluviómetros de la cuenca")
    p.add_argument("--red", choices=("ana", "cemaden"), default="ana", help="red de pluviómetros (defecto: ana)")
    p.add_argument("--dry-run", action="store_true", help="mostrar el resultado sin escribir el archivo")
    p.set_defaults(func=cmd_pluviometros)

    p = sub.add_parser("subcuencas", help="delimitar las áreas incrementales por punto de control")
    p.add_argument("--workdir", help="directorio para los rasters intermedios (se reutiliza)")
    p.set_defaults(func=cmd_subcuencas)

    p = sub.add_parser("html", help="escribir el panel HTML de la cascada")
    p.add_argument("--out", default="panel/iguacu.html", help="archivo de salida")
    p.add_argument("--demo", action="store_true",
                   help="usar datos de ejemplo en vez del almacenamiento local")
    p.add_argument("--fragment", action="store_true",
                   help="sin <html>/<head>/<body>, para incrustar o publicar")
    p.set_defaults(func=cmd_html)

    p = sub.add_parser("lluvia", help="escribir sólo la lluvia por subcuenca, como salida aparte")
    p.add_argument("--out", default="panel/lluvia.html", help="archivo de salida")
    p.add_argument("--ventana", type=int, default=72, help="horas que muestra el mapa (defecto: 72)")
    p.add_argument("--fragment", action="store_true",
                   help="sin <html>/<head>/<body>, para incrustar o publicar")
    p.set_defaults(func=cmd_lluvia)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
