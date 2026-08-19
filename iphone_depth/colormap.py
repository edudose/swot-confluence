"""Depth visualization ramp: piecewise-linear blue -> cyan -> green -> yellow
-> orange -> red (anchors approximating Google's Turbo ramp).

Self-contained so we don't need matplotlib just to colorize a depth map.
"""

from __future__ import annotations

import numpy as np

_ANCHORS = np.array([
    (48, 18, 59),     # 0.000 dark blue
    (65, 105, 225),   # 0.143 blue
    (26, 188, 229),   # 0.286 cyan
    (36, 231, 160),   # 0.429 green
    (170, 250, 65),   # 0.571 yellow-green
    (249, 189, 38),   # 0.714 yellow-orange
    (234, 88, 10),    # 0.857 orange-red
    (122, 4, 3),      # 1.000 dark red
], dtype=np.float64)


def turbo(x: np.ndarray) -> np.ndarray:
    """Map values in [0, 1] to RGB uint8 (..., 3)."""
    x = np.clip(np.asarray(x, dtype=np.float64), 0.0, 1.0)
    stops = np.linspace(0.0, 1.0, len(_ANCHORS))
    channels = [np.interp(x, stops, _ANCHORS[:, c]) for c in range(3)]
    return (np.stack(channels, axis=-1) + 0.5).astype(np.uint8)
