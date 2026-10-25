"""Delimitación de áreas incrementales por punto de la cascada.

Genera, a partir de un modelo de elevación, el área que drena a cada punto
de control (usinas y estaciones del tronco) descontando la de los puntos
aguas arriba. Es la división que sirve para leer la operación: la lluvia
sobre el área incremental de una usina es la que le llega como aporte
lateral, sin pasar por la usina anterior.

Se corre una vez (`iguacu-monitor subcuencas`) y el resultado, un GeoJSON,
queda versionado: el monitor de rutina sólo lo lee y no necesita las
dependencias de esta etapa (rasterio, pysheds), que van en el extra `geo`.

Modelo de elevación: Copernicus GLO-90 (bucket público en AWS), remuestreado
a una resolución configurable; 6" (~180 m) alcanza para áreas de miles de km².

Dos caminos:

* `delineate_fdir` (el que se usa): direcciones de flujo ya condicionadas
  hidrológicamente (HydroSHEDS v1, 15"), con la red de ríos incorporada. No
  dependen de rellenar depresiones sobre un modelo de superficie.
* `delineate` sobre Copernicus: NO pasa la validación contra ANA (-10 % en
  Capanema, -26 % en União da Vitória). Copernicus es un modelo de
  superficie: el dosel de la planicie entre São Mateus do Sul y Fluviópolis
  actúa como dique y desvía el alto Iguazú hacia otra cuenca, y las presas y
  los espejos de agua cortan la red aguas abajo. Se conserva como referencia.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

COPERNICUS = ("https://copernicus-dem-90m.s3.amazonaws.com/"
              "Copernicus_DSM_COG_30_{tile}_DEM/Copernicus_DSM_COG_30_{tile}_DEM.tif")
HYDROSHEDS_DIR_15S = "https://data.hydrosheds.org/file/hydrosheds-v1-dir/hyd_sa_dir_15s.zip"
EARTH_RADIUS_KM = 6371.0088


@dataclass
class Outlet:
    key: str
    name: str
    lon: float
    lat: float
    area_ref_km2: float | None = None   # área de drenaje oficial (ANA), para validar


def tiles_for(bbox: tuple[float, float, float, float]) -> list[str]:
    """Nombres de mosaico Copernicus (esquina inferior izquierda) que cubren la caja."""
    lon_min, lat_min, lon_max, lat_max = bbox
    names = []
    for lat in range(math.floor(lat_min), math.ceil(lat_max)):
        for lon in range(math.floor(lon_min), math.ceil(lon_max)):
            ns = f"{'S' if lat < 0 else 'N'}{abs(lat):02d}_00"
            ew = f"{'W' if lon < 0 else 'E'}{abs(lon):03d}_00"
            names.append(f"{ns}_{ew}")
    return names


def download_tiles(bbox, dest: Path) -> list[Path]:
    from iguacu_monitor import http

    dest.mkdir(parents=True, exist_ok=True)
    paths = []
    for tile in tiles_for(bbox):
        path = dest / f"Copernicus_DSM_COG_30_{tile}_DEM.tif"
        if not path.exists() or path.stat().st_size == 0:
            resp = http.get(COPERNICUS.format(tile=tile), timeout=600)
            if resp.status_code == 404:
                continue  # mar o fuera de cobertura
            resp.raise_for_status()
            path.write_bytes(resp.content)
        paths.append(path)
    return paths


def build_dem(tiles: list[Path], bbox, res_deg: float, out: Path) -> Path:
    """Mosaico de los mosaicos, recortado a la caja y remuestreado (promedio)."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.merge import merge

    sources = [rasterio.open(p) for p in tiles]
    try:
        mosaic, transform = merge(sources, bounds=bbox, res=res_deg,
                                  resampling=Resampling.average, nodata=-9999)
        profile = sources[0].profile.copy()
    finally:
        for src in sources:
            src.close()
    profile.update(driver="GTiff", height=mosaic.shape[1], width=mosaic.shape[2],
                   transform=transform, count=1, dtype="float32", nodata=-9999,
                   compress="deflate", tiled=False)
    profile.pop("blockxsize", None)
    profile.pop("blockysize", None)
    with rasterio.open(out, "w", **profile) as dst:
        dst.write(mosaic[0].astype("float32"), 1)
    return out


def cell_area_km2(transform, height: int):
    """Área de celda por fila, en km², para una grilla geográfica."""
    import numpy as np

    dlon = math.radians(abs(transform.a))
    dlat = abs(transform.e)
    top = transform.f
    lat_n = top - dlat * np.arange(height)
    lat_s = lat_n - dlat
    band = np.abs(np.sin(np.radians(lat_n)) - np.sin(np.radians(lat_s)))
    return EARTH_RADIUS_KM ** 2 * dlon * band


def delineate(dem_path: Path, outlets: list[Outlet], snap_min_km2: float = 10_000):
    """Devuelve (etiquetas, transform, resumen) de las áreas incrementales.

    Cada celda recibe el índice (1..n) del punto de control más aguas arriba
    que la drena, o 0 si queda fuera de la cuenca del último punto. Los
    puntos se ajustan a celdas con área acumulada >= snap_min_km2 para que
    caigan en el cauce principal y no en un afluente vecino.
    """
    import numpy as np

    # pysheds todavía llama a np.in1d, que numpy 2 eliminó; np.isin es el
    # reemplazo oficial con la misma semántica.
    if not hasattr(np, "in1d"):
        np.in1d = np.isin
    from pysheds.grid import Grid
    from pysheds.sview import Raster

    grid = Grid.from_raster(str(dem_path))
    dem = grid.read_raster(str(dem_path))
    dem = grid.fill_pits(dem)
    dem = grid.fill_depressions(dem)
    dem = grid.resolve_flats(dem)
    fdir = grid.flowdir(dem)
    acc = grid.accumulation(fdir)

    rows_area = cell_area_km2(grid.affine, acc.shape[0])
    area_acc = np.asarray(acc) * rows_area[:, None]     # aproximación por fila
    mask = Raster(area_acc >= snap_min_km2, viewfinder=acc.viewfinder)

    # De aguas abajo hacia aguas arriba: al final cada celda queda con el
    # punto más aguas arriba que la contiene, que es su área incremental.
    order = sorted(range(len(outlets)), key=lambda i: outlets[i].area_ref_km2 or 0, reverse=True)
    labels = np.zeros(acc.shape, dtype=np.int16)
    summary = []
    for i in order:
        o = outlets[i]
        x, y = grid.snap_to_mask(mask, (o.lon, o.lat))
        catch = np.asarray(grid.catchment(x=x, y=y, fdir=fdir, xytype="coordinate"), dtype=bool)
        total = float((catch * rows_area[:, None]).sum())
        labels[catch] = i + 1
        summary.append({"key": o.key, "snapped": (float(x), float(y)),
                        "area_km2": total, "area_ref_km2": o.area_ref_km2})
    summary.sort(key=lambda s: s["area_km2"])
    for i, o in enumerate(outlets):
        incr = float(((labels == i + 1) * rows_area[:, None]).sum())
        next(s for s in summary if s["key"] == o.key)["incremental_km2"] = incr
    return labels, grid.affine, summary


def to_geojson(labels, transform, outlets: list[Outlet], summary, tolerance_deg: float = 0.004) -> dict:
    """Polígonos simplificados de cada área incremental, más el contorno."""
    import numpy as np
    from rasterio.features import shapes
    from shapely.geometry import mapping, shape
    from shapely.ops import unary_union

    pieces: dict[int, list] = {}
    for geom, value in shapes(labels.astype(np.int32), mask=labels > 0, transform=transform):
        pieces.setdefault(int(value), []).append(shape(geom))

    by_key = {s["key"]: s for s in summary}
    features = []
    for i, o in enumerate(outlets):
        parts = pieces.get(i + 1)
        if not parts:
            continue
        geom = unary_union(parts)
        # Quedarse con la parte principal: los píxeles sueltos son ruido.
        if geom.geom_type == "MultiPolygon":
            geom = max(geom.geoms, key=lambda g: g.area)
        geom = geom.simplify(tolerance_deg, preserve_topology=True)
        s = by_key[o.key]
        features.append({
            "type": "Feature",
            "properties": {
                "id": o.key, "name": o.name, "order": i + 1,
                "incremental_km2": round(s["incremental_km2"]),
                "drainage_km2": round(s["area_km2"]),
                "drainage_ref_km2": o.area_ref_km2,
                "drainage_error": (round(s["area_km2"] / o.area_ref_km2 - 1, 4)
                                   if o.area_ref_km2 else None),
                "outlet": [round(o.lon, 4), round(o.lat, 4)],
            },
            "geometry": mapping(geom),
        })
    return {"type": "FeatureCollection", "features": features}


# ------------------------------------------------- direcciones condicionadas
def crop_raster(src_path: Path, bbox, out: Path) -> Path:
    """Recorta un raster a la caja y lo escribe como GeoTIFF."""
    import rasterio
    from rasterio.windows import from_bounds

    with rasterio.open(src_path) as src:
        window = from_bounds(*bbox, src.transform).round_offsets().round_lengths()
        data = src.read(1, window=window)
        profile = src.profile.copy()
        profile.update(driver="GTiff", height=data.shape[0], width=data.shape[1],
                       transform=src.window_transform(window), compress="deflate")
        profile.pop("blockxsize", None)
        profile.pop("blockysize", None)
        profile["tiled"] = False
    with rasterio.open(out, "w", **profile) as dst:
        dst.write(data, 1)
    return out


def snap_to_main_stem(up_km2, transform, lon: float, lat: float, area_ref_km2: float | None,
                      min_frac: float = 0.5, max_radius_deg: float = 0.15):
    """Celda del cauce principal más cercana al punto.

    "Cauce principal" = celdas que drenan al menos `min_frac` del área de
    referencia. Se elige la MÁS CERCANA, no la de área más parecida: así la
    comparación posterior contra el área oficial sigue siendo una prueba y
    no queda forzada por el propio ajuste.
    """
    import numpy as np

    threshold = (area_ref_km2 or 0) * min_frac
    col0 = (lon - transform.c) / transform.a
    row0 = (lat - transform.f) / transform.e
    radius = int(math.ceil(max_radius_deg / abs(transform.a)))
    r0, r1 = max(int(row0) - radius, 0), min(int(row0) + radius + 1, up_km2.shape[0])
    c0, c1 = max(int(col0) - radius, 0), min(int(col0) + radius + 1, up_km2.shape[1])
    window = up_km2[r0:r1, c0:c1]
    rows, cols = np.where(window >= threshold)
    if rows.size == 0:
        raise ValueError(f"sin cauce con >= {threshold:,.0f} km² a menos de {max_radius_deg}° de ({lon}, {lat})")
    rows, cols = rows + r0, cols + c0
    dist = np.hypot(rows + 0.5 - row0, cols + 0.5 - col0)
    k = int(np.argmin(dist))
    row, col = int(rows[k]), int(cols[k])
    x = transform.c + (col + 0.5) * transform.a
    y = transform.f + (row + 0.5) * transform.e
    km = float(dist[k]) * abs(transform.a) * 111.32
    return x, y, km, row, col


def delineate_fdir(fdir_path: Path, outlets: list[Outlet], min_frac: float = 0.5):
    """Áreas incrementales a partir de direcciones de flujo D8 ya condicionadas.

    Devuelve (etiquetas, transform, resumen). Mismo criterio de etiquetado que
    `delineate`: cada celda queda con el punto más aguas arriba que la drena.
    """
    import numpy as np

    if not hasattr(np, "in1d"):          # pysheds con numpy 2
        np.in1d = np.isin
    from pysheds.grid import Grid

    grid = Grid.from_raster(str(fdir_path))
    fdir = grid.read_raster(str(fdir_path))
    acc = grid.accumulation(fdir)
    rows_area = cell_area_km2(grid.affine, acc.shape[0])
    up_km2 = np.asarray(acc, dtype="float64") * rows_area[:, None]

    order = sorted(range(len(outlets)), key=lambda i: outlets[i].area_ref_km2 or 0, reverse=True)
    labels = np.zeros(acc.shape, dtype=np.int16)
    summary = []
    for i in order:
        o = outlets[i]
        x, y, km, row, col = snap_to_main_stem(up_km2, grid.affine, o.lon, o.lat,
                                               o.area_ref_km2, min_frac)
        # Por índice y no por coordenada: con el centro exacto de la celda,
        # pysheds puede redondear a la vecina y trazar un arroyo lateral.
        catch = np.asarray(grid.catchment(x=col, y=row, fdir=fdir, xytype="index"), dtype=bool)
        total = float((catch * rows_area[:, None]).sum())
        labels[catch] = i + 1
        summary.append({"key": o.key, "snapped": (x, y), "snap_km": km,
                        "area_km2": total, "area_ref_km2": o.area_ref_km2})
    for i, o in enumerate(outlets):
        incr = float(((labels == i + 1) * rows_area[:, None]).sum())
        next(s for s in summary if s["key"] == o.key)["incremental_km2"] = incr
    summary.sort(key=lambda s: s["area_km2"])
    return labels, grid.affine, summary
