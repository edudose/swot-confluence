"""EXIF summary and camera classification for iPhone photos."""

from __future__ import annotations

from typing import Any

from PIL import ExifTags, Image

_MAIN_TAGS = (
    "Make", "Model", "Software", "DateTimeOriginal", "LensModel", "LensMake",
    "FocalLength", "FocalLengthIn35mmFilm", "FNumber", "ExposureTime",
    "ISOSpeedRatings", "PhotographicSensitivity", "Orientation",
    "ExifImageWidth", "ExifImageHeight", "SubjectArea",
)


def exif_summary(im: Image.Image) -> dict[str, Any]:
    """Flatten IFD0 + Exif IFD into a {tag_name: value} dict."""
    exif = im.getexif()
    flat: dict[str, Any] = {}
    for tag_id, value in exif.items():
        flat[ExifTags.TAGS.get(tag_id, hex(tag_id))] = value
    try:
        for tag_id, value in exif.get_ifd(ExifTags.IFD.Exif).items():
            flat.setdefault(ExifTags.TAGS.get(tag_id, hex(tag_id)), value)
    except Exception:
        pass
    out = {}
    for k in _MAIN_TAGS:
        if k in flat:
            out[k] = _plain(flat[k])
    out["_total_exif_tags"] = len(flat)
    return out


def _plain(v: Any) -> Any:
    if isinstance(v, bytes):
        return v.decode("ascii", "replace")
    if isinstance(v, (tuple, list)):
        return [_plain(x) for x in v]
    try:
        return float(v) if v.__class__.__name__ == "IFDRational" else v
    except Exception:
        return str(v)


def classify_camera(summary: dict[str, Any]) -> str:
    """Front (TrueDepth) vs rear (LiDAR-assisted on Pro) heuristic."""
    lens = str(summary.get("LensModel", "")).lower()
    if "front" in lens:
        return "frontal (TrueDepth: luz estructurada IR, sin LiDAR)"
    if "back" in lens:
        return "trasera (en modelos Pro el Retrato usa asistencia LiDAR)"
    return "desconocida (EXIF sin datos de lente)"


def focal35(summary: dict[str, Any]) -> float | None:
    v = summary.get("FocalLengthIn35mmFilm")
    try:
        return float(v) if v else None
    except (TypeError, ValueError):
        return None
