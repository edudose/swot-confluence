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
    ana_code: int | None = None
    order: int = 0
    role: str = ""
    river: str = ""


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
            order=int(p.get("order", 0)),
        ))
    for s in raw.get("stations") or []:
        code = s.get("ana_code")
        sites.append(Site(
            key=s["key"], name=s["name"], kind="station",
            copel_label=s.get("copel_label"),
            ana_code=int(code) if code is not None else None,
            role=s.get("role", ""), river=s.get("river", ""),
        ))

    store_path = Path((raw.get("store") or {}).get("path", "data/iguacu.parquet"))
    return Config(
        basin=basin,
        sites=sites,
        sources=raw.get("sources") or {},
        store_path=store_path,
    )
