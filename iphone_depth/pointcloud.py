"""Back-project a depth map into a colored PLY point cloud.

Camera model: pinhole with square pixels, principal point at the image
center. When only the 35mm-equivalent focal length is known:

    fx = fy = f35 * long_side_px / 36.0

(36mm is the long side of a full frame; a fair approximation for iPhone.)
Convention: camera looks down +Z, X right, Y down (image coordinates).
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
from PIL import Image


def intrinsics_from_f35(width: int, height: int, f35: float) -> tuple[float, float, float, float]:
    f_px = f35 * max(width, height) / 36.0
    return f_px, f_px, width / 2.0, height / 2.0


def backproject(depth: np.ndarray, fx: float, fy: float, cx: float, cy: float,
                rgb: np.ndarray | None = None, stride: int = 1,
                max_depth: float | None = None):
    """Return (points Nx3 float32, colors Nx3 uint8|None)."""
    h, w = depth.shape
    us, vs = np.meshgrid(np.arange(w), np.arange(h))
    d = depth[::stride, ::stride]
    us = us[::stride, ::stride]
    vs = vs[::stride, ::stride]
    mask = np.isfinite(d) & (d > 0)
    if max_depth:
        mask &= d <= max_depth
    z = d[mask]
    x = (us[mask] - cx) * z / fx
    y = (vs[mask] - cy) * z / fy
    pts = np.stack([x, y, z], axis=-1).astype(np.float32)
    colors = None
    if rgb is not None:
        colors = rgb[::stride, ::stride][mask].astype(np.uint8)
    return pts, colors


def write_ply(path: Path, points: np.ndarray, colors: np.ndarray | None = None) -> int:
    """Binary little-endian PLY. Returns the number of vertices written."""
    n = len(points)
    header = ["ply", "format binary_little_endian 1.0", f"element vertex {n}",
              "property float x", "property float y", "property float z"]
    if colors is not None:
        header += ["property uchar red", "property uchar green", "property uchar blue"]
    header.append("end_header")
    with open(path, "wb") as f:
        f.write(("\n".join(header) + "\n").encode("ascii"))
        if colors is None:
            f.write(points.astype("<f4").tobytes())
        else:
            rec = np.zeros(n, dtype=[("xyz", "<f4", 3), ("rgb", "u1", 3)])
            rec["xyz"] = points
            rec["rgb"] = colors
            f.write(rec.tobytes())
    return n


def depth_to_ply(depth: np.ndarray, out_path: Path, f35: float,
                 photo: Image.Image | None = None, stride: int = 2,
                 max_depth: float | None = None) -> int:
    h, w = depth.shape
    rgb = None
    if photo is not None:
        rgb = np.asarray(photo.convert("RGB").resize((w, h), Image.BILINEAR))
    fx, fy, cx, cy = intrinsics_from_f35(w, h, f35)
    pts, colors = backproject(depth, fx, fy, cx, cy, rgb, stride, max_depth)
    return write_ply(out_path, pts, colors)
