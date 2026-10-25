"""Subcuencas: carga de la capa y promedios areales sobre grillas.

La capa es un GeoJSON con un polígono por subcuenca y propiedades `id` y
`name` (las genera `iguacu-monitor subcuencas`, pero sirve cualquier capa con
esas dos propiedades, por ejemplo las subcuencas de un modelo HMS). Sólo
requiere shapely, liviano; no las dependencias de delimitación.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SubBasin:
    id: str
    name: str
    order: int
    area_km2: float
    geom: object  # shapely geometry

    @property
    def key(self) -> str:
        return f"sb_{self.id}"


def load(path: str | Path) -> list[SubBasin]:
    from shapely.geometry import shape

    path = Path(path)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for i, feat in enumerate(data.get("features") or [], start=1):
        props = feat.get("properties") or {}
        out.append(SubBasin(
            id=str(props.get("id", i)), name=str(props.get("name", props.get("id", i))),
            order=int(props.get("order", i)),
            area_km2=float(props.get("incremental_km2") or 0.0),
            geom=shape(feat["geometry"]),
        ))
    return sorted(out, key=lambda s: s.order)


def outline(subs: list[SubBasin]):
    from shapely.ops import unary_union

    return unary_union([s.geom for s in subs]) if subs else None


def grid_masks(lons, lats, subs: list[SubBasin]) -> dict[str, "object"]:
    """Máscara booleana (lat x lon) de los centros de celda dentro de cada subcuenca."""
    import numpy as np
    import shapely

    lon2, lat2 = np.meshgrid(lons, lats)
    return {s.id: shapely.contains_xy(s.geom, lon2, lat2) for s in subs}


def areal_means(values, lons, lats, subs: list[SubBasin]) -> dict[str, float]:
    """Promedio de una grilla (lat x lon) dentro de cada subcuenca y del contorno.

    Pondera por área de celda (cos lat), que en una grilla geográfica no es
    uniforme. Devuelve claves `basin` y `sb_<id>`; omite las vacías.
    """
    import numpy as np

    values = np.asarray(values, dtype="float64")
    weights = np.cos(np.radians(np.asarray(lats)))[:, None] * np.ones((1, len(lons)))
    masks = grid_masks(lons, lats, subs)
    out: dict[str, float] = {}
    total_w, total_v = 0.0, 0.0
    for s in subs:
        m = masks[s.id] & np.isfinite(values)
        if not m.any():
            continue
        w = weights[m]
        out[s.key] = float((values[m] * w).sum() / w.sum())
        total_w += float(w.sum())
        total_v += float((values[m] * w).sum())
    if total_w > 0:
        out["basin"] = total_v / total_w
    return out


def idw_grid(points_lon, points_lat, values, lons, lats, power: float = 2.0):
    """Interpolación por distancia inversa sobre una grilla (lat x lon).

    Distancias planas con escala cos(lat): a la escala de la cuenca el error
    frente a la geodésica es despreciable.
    """
    import numpy as np

    px = np.asarray(points_lon, dtype="float64")
    py = np.asarray(points_lat, dtype="float64")
    pv = np.asarray(values, dtype="float64")
    lon2, lat2 = np.meshgrid(lons, lats)
    k = math.cos(math.radians(float(np.mean(lats))))
    dx = (lon2[..., None] - px) * k
    dy = lat2[..., None] - py
    d2 = dx * dx + dy * dy
    exact = d2 < 1e-12
    w = 1.0 / np.power(np.maximum(d2, 1e-12), power / 2.0)
    grid = (w * pv).sum(axis=-1) / w.sum(axis=-1)
    if exact.any():
        idx = exact.argmax(axis=-1)
        grid = np.where(exact.any(axis=-1), pv[idx], grid)
    return grid
