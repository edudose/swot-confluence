"""INPE/CPTEC MERGE — precipitación diaria en grilla sobre Sudamérica.

MERGE combina estimaciones satelitales GPM con pluviómetros de superficie.
Resolución 0,1°, serie diaria desde junio de 2000, archivos GRIB2 en:

    {base}/{YYYY}/{MM}/MERGE_CPTEC_{YYYYMMDD}.grib2

El monitor promedia la grilla sobre la cuenca y devuelve un valor diario de
lámina media areal, que es el número que sirve para leer la respuesta
hidrológica. Si hay un polígono configurado se recorta con él; si no, se usa
la caja envolvente, que sobreestima al incluir áreas vecinas.

Leer GRIB2 requiere `cfgrib` (sobre eccodes) y `xarray`. Son dependencias
pesadas, así que se importan de forma perezosa y con un mensaje accionable.
"""

from __future__ import annotations

import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from iguacu_monitor import http
from iguacu_monitor.model import PRECIP, Observation, ProbeResult
from iguacu_monitor.sources.base import Source

BASIN_SITE = "basin"  # clave del promedio areal


class MissingGribSupport(RuntimeError):
    pass


class NotPublished(http.FetchError):
    """El archivo del día aún no existe en el servidor."""


def _require_grib():
    try:
        import xarray as xr  # noqa: F401
        import cfgrib  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depende del entorno
        raise MissingGribSupport(
            "falta soporte GRIB2. Instalá:  pip install xarray cfgrib eccodes\n"
            "(en Windows puede hacer falta:  conda install -c conda-forge cfgrib eccodes)"
        ) from exc
    import xarray as xr
    return xr


class InpeMerge(Source):
    name = "inpe_merge"

    def url_for(self, day: date) -> str:
        base = self.opts.get("base", "https://ftp.cptec.inpe.br/modelos/tempo/MERGE/GPM/DAILY")
        pattern = self.opts.get("pattern", "{base}/{yyyy}/{mm}/MERGE_CPTEC_{yyyymmdd}.grib2")
        return pattern.format(
            base=base, yyyy=f"{day:%Y}", mm=f"{day:%m}",
            dd=f"{day:%d}", yyyymmdd=f"{day:%Y%m%d}",
        )

    def _download(self, day: date, dest: Path) -> int:
        resp = http.get(self.url_for(day), timeout=self.opts.get("timeout", 180), stream=True)
        if resp.status_code == 404:
            # MERGE publica con rezago: el día en curso suele no existir aún.
            raise NotPublished(f"MERGE {day:%Y-%m-%d} todavía no publicado")
        resp.raise_for_status()
        size = 0
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if chunk:
                    fh.write(chunk)
                    size += len(chunk)
        return size

    def basin_mean(self, grib_path: Path) -> float:
        """Lámina media areal sobre la cuenca, en mm."""
        xr = _require_grib()
        lon_min, lat_min, lon_max, lat_max = self.cfg.basin.bbox

        with xr.open_dataset(grib_path, engine="cfgrib",
                             backend_kwargs={"indexpath": ""}) as ds:
            var = next(iter(ds.data_vars))
            da = ds[var]

            # MERGE publica longitudes en 0..360; la cuenca está en negativos.
            lon_name = "longitude" if "longitude" in da.coords else "lon"
            lat_name = "latitude" if "latitude" in da.coords else "lat"
            lons = da[lon_name]
            if float(lons.max()) > 180:
                da = da.assign_coords({lon_name: (((lons + 180) % 360) - 180)})
                da = da.sortby(lon_name)

            # La latitud puede venir descendente: ordenar antes de cortar.
            if float(da[lat_name][0]) > float(da[lat_name][-1]):
                da = da.sortby(lat_name)

            clip = da.sel({lon_name: slice(lon_min, lon_max),
                           lat_name: slice(lat_min, lat_max)})
            if clip.size == 0:
                raise ValueError(
                    f"el recorte quedó vacío para bbox={self.cfg.basin.bbox}; "
                    f"rango del archivo: lon[{float(da[lon_name].min()):.2f},"
                    f"{float(da[lon_name].max()):.2f}] "
                    f"lat[{float(da[lat_name].min()):.2f},{float(da[lat_name].max()):.2f}]"
                )
            return float(clip.mean().values)

    def probe(self) -> ProbeResult:
        # MERGE se publica con rezago; se prueba anteayer para no chocar con
        # el archivo del día en curso, que puede no existir todavía.
        day = date.today() - timedelta(days=2)
        result = ProbeResult(source=self.name, ok=False, url=self.url_for(day))
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "merge.grib2"
            try:
                size = self._download(day, dest)
            except Exception as exc:  # noqa: BLE001
                result.error = str(exc)
                return result
            result.http_status = 200
            result.size_bytes = size
            result.content_type = "application/grib2"
            try:
                mean = self.basin_mean(dest)
            except MissingGribSupport as exc:
                result.notes.append(str(exc))
                result.ok = True  # la descarga sí funcionó
                result.notes.append("descarga OK; falta la librería para leerlo")
                return result
            except Exception as exc:  # noqa: BLE001
                result.error = f"descarga OK pero no se pudo leer: {exc}"
                return result

            result.parsed = 1
            result.ok = True
            result.sample = f"{day:%Y-%m-%d}  lámina media areal = {mean:.2f} mm"
        return result

    def fetch(self, start: date, end: date) -> list[Observation]:
        out: list[Observation] = []
        day = start
        with tempfile.TemporaryDirectory() as tmp:
            while day <= end:
                dest = Path(tmp) / f"merge_{day:%Y%m%d}.grib2"
                try:
                    self._download(day, dest)
                    mean = self.basin_mean(dest)
                except http.FetchError:
                    day += timedelta(days=1)   # día aún no publicado
                    continue
                out.append(Observation(
                    source=self.name, site=BASIN_SITE, variable=PRECIP,
                    # El acumulado diario de MERGE se sella al cierre del día.
                    timestamp=datetime.combine(day, time(12, 0), tzinfo=timezone.utc),
                    value=mean,
                    meta={"bbox": list(self.cfg.basin.bbox)},
                ))
                day += timedelta(days=1)
        return out
