"""CLI del toolkit de profundidad iPhone.

Uso:
    python -m iphone_depth diagnose FOTO.heic
    python -m iphone_depth extract FOTO.heic --out salida/
    python -m iphone_depth pointcloud --depth salida/FOTO_disparity.npy \
        --photo FOTO.heic --f35 26 --out nube.ply [--dmin 0.3 --dmax 8.0]
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

from . import depthmap, exifdump, heic, jpeg_segments, pointcloud

HEIC_BRANDS = (b"heic", b"heix", b"hevc", b"heif", b"mif1", b"msf1")


def sniff(path: Path) -> str:
    head = path.read_bytes()[:32]
    if head[:2] == b"\xff\xd8":
        return "jpeg"
    if head[4:8] == b"ftyp" and head[8:12] in HEIC_BRANDS:
        return "heic"
    return "otro"


def _exif_block(im: Image.Image) -> dict:
    summary = exifdump.exif_summary(im)
    summary["_camara"] = exifdump.classify_camera(summary)
    return summary


def _analyze(path: Path) -> dict:
    """Shared analysis used by diagnose and extract."""
    kind = sniff(path)
    info: dict = {"archivo": str(path), "formato": kind, "profundidad": False,
                  "detalles": [], "aux": []}
    if kind == "jpeg":
        data = path.read_bytes()
        rep = jpeg_segments.inspect_jpeg(data)
        info["segmentos_app"] = [f"{hex(s.marker)}:{s.ident or '-'}" for s in rep.segments]
        info["mpf"] = rep.has_mpf
        info["_jpeg_report"] = rep
        info["_jpeg_data"] = data
        embedded = jpeg_segments.extract_embedded(data, rep)
        info["imagenes_embebidas"] = len(embedded)
        info["_embedded"] = embedded
        if rep.has_mpf and embedded:
            info["profundidad"] = True
            info["detalles"].append(
                f"MPF presente con {len(embedded)} imagen(es) embebida(s): "
                + ", ".join(e.type_name for e in rep.mp_entries if e.offset))
        elif embedded:
            info["detalles"].append(
                f"{len(embedded)} imagen(es) embebida(s) tras el JPEG principal (sin índice MPF)")
            info["profundidad"] = True
        else:
            info["detalles"].append(
                "Sin segmento MPF ni imágenes embebidas: el mapa de profundidad "
                "fue eliminado al exportar/compartir la foto.")
        info["exif"] = _exif_block(Image.open(path))
    elif kind == "heic":
        hf = heic.open_heic(str(path))
        results = heic.extract_all(hf)
        info["_heic_results"] = results
        info["aux"] = [f"{r.kind}:{r.label}" for r in results]
        depth_like = [r for r in results if r.kind == "depth"
                      or r.label in ("disparity", "depth")]
        if depth_like:
            info["profundidad"] = True
            r = depth_like[0]
            info["detalles"].append(f"Mapa de profundidad/disparidad de {r.image.size[0]}x{r.image.size[1]} px")
            if r.metadata:
                info["metadatos_profundidad"] = {k: str(v) for k, v in r.metadata.items()}
        elif results:
            info["detalles"].append("Hay imágenes auxiliares pero ninguna de profundidad "
                                    "(p. ej. solo gain map HDR o mattes).")
        else:
            info["detalles"].append("El HEIC no contiene imágenes auxiliares ni de profundidad.")
        info["exif"] = _exif_block(Image.open(path))
    else:
        info["detalles"].append("Formato no reconocido (se esperaba JPEG o HEIC).")
    return info


def cmd_diagnose(args: argparse.Namespace) -> int:
    info = _analyze(Path(args.foto))
    print(f"Archivo : {info['archivo']}")
    print(f"Formato : {info['formato']}")
    exif = info.get("exif") or {}
    if exif:
        if exif.get("Make") or exif.get("Model"):
            print(f"Cámara  : {exif.get('Make', '?')} {exif.get('Model', '?')} — "
                  f"{exif.get('LensModel', 'lente ?')} [{exif['_camara']}]")
            print(f"EXIF    : {exif.get('_total_exif_tags', 0)} etiquetas")
        else:
            print("Cámara  : sin identificación (marca/modelo/lente eliminados del EXIF —")
            print("          la foto fue re-procesada al compartirla o subirla)")
    if "segmentos_app" in info:
        print(f"Segmentos JPEG: {', '.join(info['segmentos_app'])}")
        print(f"Índice MPF    : {'sí' if info['mpf'] else 'no'}")
    if info.get("aux"):
        print(f"Auxiliares HEIC: {', '.join(info['aux'])}")
    print()
    for d in info["detalles"]:
        print(f"  - {d}")
    if info.get("metadatos_profundidad"):
        print(f"  - Metadatos de profundidad: {info['metadatos_profundidad']}")
    print()
    if info["profundidad"]:
        print("VEREDICTO: SÍ hay datos de profundidad. Usá `extract` para sacarlos.")
        return 0
    print("VEREDICTO: NO hay datos de profundidad en este archivo.")
    print("  Para conservarlos: comprimí el .HEIC original en un .zip antes de")
    print("  enviarlo, o exportá con 'original sin modificar' (ver README).")
    return 2


def cmd_extract(args: argparse.Namespace) -> int:
    path = Path(args.foto)
    out_dir = Path(args.out)
    info = _analyze(path)
    stem = path.stem
    products: dict[str, str] = {}
    metric = {"d_min": None, "d_max": None, "metrico": False}

    if info["formato"] == "heic":
        for r in info.get("_heic_results", []):
            if r.kind == "depth" or r.label in ("disparity", "depth"):
                norm = depthmap.to_float(r.image)
                products.update(depthmap.save_products(norm, out_dir, stem))
                d_min, d_max = depthmap.metric_range(r.metadata)
                metric = {"d_min": d_min, "d_max": d_max,
                          "metrico": d_min is not None and d_max is not None}
            else:
                out_dir.mkdir(parents=True, exist_ok=True)
                p = out_dir / f"{stem}_{r.label}.png"
                r.image.save(p)
                products[r.label] = str(p)
    elif info["formato"] == "jpeg":
        for i, blob in enumerate(info.get("_embedded", []), start=1):
            try:
                im = Image.open(io.BytesIO(blob))
                im.load()
            except Exception:
                continue
            low = blob[:4096].lower()
            label = ("disparity" if b"disparity" in low or im.mode in ("L", "I;16")
                     else f"embedded_{i}")
            if label == "disparity" and "disparity_npy" not in products:
                norm = depthmap.to_float(im)
                products.update(depthmap.save_products(norm, out_dir, stem))
            else:
                out_dir.mkdir(parents=True, exist_ok=True)
                p = out_dir / f"{stem}_{label}.png"
                im.save(p)
                products[label] = str(p)

    meta = {k: v for k, v in info.items() if not k.startswith("_")}
    meta["productos"] = products
    meta["escala"] = metric
    out_dir.mkdir(parents=True, exist_ok=True)
    meta_path = out_dir / f"{stem}_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False, default=str))
    print(json.dumps(meta, indent=2, ensure_ascii=False, default=str))

    if not products:
        print("\nNo se extrajo nada: el archivo no trae datos de profundidad.", file=sys.stderr)
        return 2
    print(f"\nListo: {len(products)} producto(s) en {out_dir}/")
    return 0


def cmd_pointcloud(args: argparse.Namespace) -> int:
    dpath = Path(args.depth)
    if dpath.suffix == ".npy":
        norm = np.load(dpath).astype(np.float32)
    else:
        norm = depthmap.to_float(Image.open(dpath))
    depth, is_metric = depthmap.disparity_to_depth(norm, args.dmin, args.dmax)
    photo = Image.open(args.photo) if args.photo else None
    n = pointcloud.depth_to_ply(depth, Path(args.out), args.f35, photo,
                                stride=args.stride, max_depth=args.max_depth)
    unit = "metros" if is_metric else "unidades relativas (sin escala métrica)"
    print(f"Nube de puntos: {n} vértices -> {args.out} [{unit}]")
    if not is_metric:
        print("  Nota: pasá --dmin/--dmax (disparidad 1/m) para escala métrica real.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="iphone_depth",
                                 description="Profundidad de fotos Retrato de iPhone")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("diagnose", help="¿Este archivo trae mapa de profundidad?")
    p.add_argument("foto")
    p.set_defaults(fn=cmd_diagnose)

    p = sub.add_parser("extract", help="Extraer profundidad, mattes y metadatos")
    p.add_argument("foto")
    p.add_argument("--out", default="salida")
    p.set_defaults(fn=cmd_extract)

    p = sub.add_parser("pointcloud", help="Depth/disparidad -> nube de puntos PLY")
    p.add_argument("--depth", required=True, help=".npy o PNG de disparidad normalizada")
    p.add_argument("--photo", help="foto RGB para colorear la nube")
    p.add_argument("--f35", type=float, default=26.0,
                   help="focal equivalente 35mm del EXIF (default 26)")
    p.add_argument("--dmin", type=float, help="disparidad mínima (1/m) para escala métrica")
    p.add_argument("--dmax", type=float, help="disparidad máxima (1/m) para escala métrica")
    p.add_argument("--stride", type=int, default=2)
    p.add_argument("--max-depth", type=float, default=None)
    p.add_argument("--out", default="nube.ply")
    p.set_defaults(fn=cmd_pointcloud)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
