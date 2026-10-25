"""Carga de la configuración del monitor."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG = Path("config/iguacu.yml")


@dataclass
class Site:
    key: str
    name: str
    kind: str                      # "plant" o "station"
    operator: str = ""
    copel_label: str | None = None
    ons_label: str | None = None
    ons_id: str | None = None
    ana_code: int | None = None
    order: int = 0
    role: str = ""
    river: str = ""
    short: str = ""
    operation: str = "caudal"      # "caudal" o "nivel" (sin umbral de caudal)
    attention: float | None = None # umbral de atención sobre el afluente [m3/s]
    alert: float | None = None     # umbral de alerta sobre el afluente [m3/s]
    travel_h: float | None = None  # tiempo de viaje desde el punto anterior [h]
    lat: float | None = None
    lon: float | None = None
    drainage_km2: float | None = None  # área de drenaje oficial (ANA)

    @property
    def label(self) -> str:
        return self.short or self.name.split(" (")[0].strip()


@dataclass
class Gauge:
    """Pluviómetro telemétrico de ANA usado para la lluvia medida."""

    code: int
    name: str
    operator: str = ""
    lat: float | None = None
    lon: float | None = None

    @property
    def key(self) -> str:
        return f"pluv_{self.code}"


@dataclass
class Basin:
    name: str
    bbox: tuple[float, float, float, float]
    polygon: str | None = None

    def contains(self, lon: float, lat: float) -> bool:
        lon_min, lat_min, lon_max, lat_max = self.bbox
        return lon_min <= lon <= lon_max and lat_min <= lat <= lat_max


@dataclass
class Config:
    basin: Basin
    sites: list[Site]
    sources: dict[str, dict[str, Any]]
    store_path: Path
    report: dict[str, Any] = field(default_factory=dict)
    gauges: list[Gauge] = field(default_factory=list)
    gauges_path: Path | None = None
    gauges_opts: dict[str, Any] = field(default_factory=dict)
    subbasins_opts: dict[str, Any] = field(default_factory=dict)

    @property
    def subbasins_path(self) -> Path | None:
        f = self.subbasins_opts.get("file")
        return Path(f) if f else None

    @property
    def plants(self) -> list[Site]:
        return sorted((s for s in self.sites if s.kind == "plant"), key=lambda s: s.order)

    @property
    def stations(self) -> list[Site]:
        return [s for s in self.sites if s.kind == "station"]

    def site(self, key: str) -> Site:
        for s in self.sites:
            if s.key == key:
                return s
        raise KeyError(f"sitio desconocido: {key}")

    def source(self, name: str) -> dict[str, Any]:
        return self.sources.get(name, {})

    def enabled(self, name: str) -> bool:
        return bool(self.sources.get(name, {}).get("enabled", False))


def _opt_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def load(path: str | Path = DEFAULT_CONFIG) -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"no encuentro la configuración en {path}. "
            f"Pasá --config con la ruta correcta."
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    basin_raw = raw.get("basin") or {}
    bbox = basin_raw.get("bbox")
    if not bbox or len(bbox) != 4:
        raise ValueError("basin.bbox debe ser [lon_min, lat_min, lon_max, lat_max]")
    basin = Basin(
        name=basin_raw.get("name", "cuenca"),
        bbox=tuple(float(v) for v in bbox),  # type: ignore[arg-type]
        polygon=basin_raw.get("polygon"),
    )

    sites: list[Site] = []
    for p in raw.get("plants") or []:
        sites.append(Site(
            key=p["key"], name=p["name"], kind="plant",
            operator=p.get("operator", ""),
            copel_label=p.get("copel_label"), ons_label=p.get("ons_label"),
            ons_id=p.get("ons_id"),
            order=int(p.get("order", 0)),
            short=p.get("short", ""),
            operation=p.get("operation", "caudal"),
            attention=_opt_float(p.get("attention")),
            alert=_opt_float(p.get("alert")),
            travel_h=_opt_float(p.get("travel_h")),
            lat=_opt_float(p.get("lat")), lon=_opt_float(p.get("lon")),
            drainage_km2=_opt_float(p.get("drainage_km2")),
        ))
        site = sites[-1]
        if site.operation not in {"caudal", "nivel"}:
            raise ValueError(f"{site.key}: operation debe ser 'caudal' o 'nivel'")
        if site.operation == "caudal" and (site.attention is None or site.alert is None):
            raise ValueError(f"{site.key}: opera por caudal pero le faltan umbrales")
        if site.attention is not None and site.alert is not None and site.alert <= site.attention:
            raise ValueError(f"{site.key}: el umbral de alerta debe superar al de atención")
    for s in raw.get("stations") or []:
        code = s.get("ana_code")
        sites.append(Site(
            key=s["key"], name=s["name"], kind="station",
            copel_label=s.get("copel_label"),
            ana_code=int(code) if code is not None else None,
            role=s.get("role", ""), river=s.get("river", ""),
            short=s.get("short", ""),
            travel_h=_opt_float(s.get("travel_h")),
            lat=_opt_float(s.get("lat")), lon=_opt_float(s.get("lon")),
            drainage_km2=_opt_float(s.get("drainage_km2")),
        ))

    store_path = Path((raw.get("store") or {}).get("path", "data/iguacu.parquet"))

    gauges_opts = raw.get("rain_gauges") or {}
    gauges_path = Path(gauges_opts["file"]) if gauges_opts.get("file") else None
    gauges: list[Gauge] = []
    if gauges_path and gauges_path.exists():
        listed = yaml.safe_load(gauges_path.read_text(encoding="utf-8")) or {}
        for g in listed.get("gauges") or []:
            gauges.append(Gauge(
                code=int(g["code"]), name=str(g.get("name", "")),
                operator=str(g.get("operator", "")),
                lat=_opt_float(g.get("lat")), lon=_opt_float(g.get("lon")),
            ))

    return Config(
        basin=basin,
        sites=sites,
        sources=raw.get("sources") or {},
        store_path=store_path,
        report=raw.get("report") or {},
        gauges=gauges,
        gauges_path=gauges_path,
        gauges_opts=gauges_opts,
        subbasins_opts=raw.get("subbasins") or {},
    )
