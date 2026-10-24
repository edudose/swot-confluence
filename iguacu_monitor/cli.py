"""Interfaz de línea de comandos del monitor."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from iguacu_monitor import config, demo, panel, report, store
from iguacu_monitor.sources.ana import ANA
from iguacu_monitor.sources.copel_mhb import CopelMHB
from iguacu_monitor.sources.inpe_merge import InpeMerge
from iguacu_monitor.sources.ons import ONS

SOURCES = {
    "copel_mhb": CopelMHB,
    "ana": ANA,
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

    p = sub.add_parser("html", help="escribir el panel HTML de la cascada")
    p.add_argument("--out", default="panel/iguacu.html", help="archivo de salida")
    p.add_argument("--demo", action="store_true",
                   help="usar datos de ejemplo en vez del almacenamiento local")
    p.add_argument("--fragment", action="store_true",
                   help="sin <html>/<head>/<body>, para incrustar o publicar")
    p.set_defaults(func=cmd_html)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
