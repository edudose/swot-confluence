"""Depth/disparity map handling: normalization, conversion and export.

Apple's Portrait maps are *disparity* (1/distance) unless the container's
depth metadata says otherwise. Without metric metadata (d_min/d_max or
z_near/z_far) the extracted map is relative: valid for visualization,
segmentation and relative geometry, but not in meters.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .colormap import turbo


def to_float(im: Image.Image) -> np.ndarray:
    """Image -> float32 array normalized to [0, 1] (single channel)."""
    arr = np.asarray(im)
    if arr.ndim == 3:
        arr = arr[..., 0]
    peak = {np.dtype(np.uint8): 255.0, np.dtype(np.uint16): 65535.0}.get(arr.dtype)
    arrf = arr.astype(np.float32)
    scale = peak if peak is not None else max(float(arrf.max()), 1.0)
    return arrf / scale


def disparity_to_depth(norm: np.ndarray, d_min: float | None = None,
                       d_max: float | None = None) -> tuple[np.ndarray, bool]:
    """Convert normalized disparity to depth.

    With d_min/d_max (1/m) the result is metric meters; otherwise a relative
    depth in (0, 1]. Returns (depth, is_metric).
    """
    if d_min is not None and d_max is not None and d_max > d_min >= 0:
        disparity = norm * (d_max - d_min) + d_min
        with np.errstate(divide="ignore"):
            depth = np.where(disparity > 0, 1.0 / disparity, 0.0)
        return depth.astype(np.float32), True
    eps = 1e-3
    depth = 1.0 / (norm * (1.0 - eps) + eps)
    return (depth / depth.max()).astype(np.float32), False


def metric_range(metadata: dict[str, Any]) -> tuple[float | None, float | None]:
    """Pull d_min/d_max (or derive from z_near/z_far) out of HEIF depth metadata."""
    d_min = metadata.get("d_min")
    d_max = metadata.get("d_max")
    if d_min is None and metadata.get("z_far"):
        d_min = 1.0 / float(metadata["z_far"])
    if d_max is None and metadata.get("z_near"):
        d_max = 1.0 / float(metadata["z_near"])
    try:
        return (float(d_min) if d_min is not None else None,
                float(d_max) if d_max is not None else None)
    except (TypeError, ValueError):
        return None, None


def save_products(norm: np.ndarray, out_dir: Path, stem: str) -> dict[str, str]:
    """Write raw 16-bit PNG, float32 .npy and a Turbo visualization."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}

    raw16 = (np.clip(norm, 0, 1) * 65535.0 + 0.5).astype(np.uint16)
    p = out_dir / f"{stem}_disparity16.png"
    Image.fromarray(raw16).save(p)
    paths["disparity16_png"] = str(p)

    p = out_dir / f"{stem}_disparity.npy"
    np.save(p, norm.astype(np.float32))
    paths["disparity_npy"] = str(p)

    p = out_dir / f"{stem}_turbo.png"
    Image.fromarray(turbo(norm)).save(p)
    paths["turbo_png"] = str(p)
    return paths
