"""Tipos compartidos por todas las fuentes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Variables normalizadas. Las fuentes traducen sus propias etiquetas a estas.
QAFL = "qafl"      # caudal afluente         [m3/s]
QDEF = "qdef"      # caudal defluente        [m3/s]
QTUR = "qtur"      # caudal turbinado        [m3/s]
QVER = "qver"      # caudal vertido          [m3/s]
QOBS = "qobs"      # caudal observado        [m3/s]
LEVEL = "level"    # nivel / cota            [m]
STORAGE = "storage"  # volumen útil          [%]
PRECIP = "precip"  # precipitación           [mm]
PRECIP_24H = "precip_24h"  # acumulado móvil de 24 h publicado como tal [mm]

UNITS = {
    QAFL: "m3/s", QDEF: "m3/s", QTUR: "m3/s", QVER: "m3/s", QOBS: "m3/s",
    LEVEL: "m", STORAGE: "%", PRECIP: "mm", PRECIP_24H: "mm",
}

VARIABLE_ALIASES: dict[str, str] = {
    # COPEL / ONS escriben estas etiquetas en portugués, con y sin acentos.
    "vazao afluente": QAFL, "afluente": QAFL, "qafl": QAFL,
    "vazao defluente": QDEF, "defluente": QDEF, "qdef": QDEF,
    "vazao turbinada": QTUR, "turbinada": QTUR, "qtur": QTUR,
    "vazao vertida": QVER, "vertida": QVER, "vertido": QVER, "qver": QVER,
    "vazao": QOBS, "vazao observada": QOBS, "descarga": QOBS,
    "nivel": LEVEL, "cota": LEVEL, "nivel montante": LEVEL,
    "volume util": STORAGE, "volume": STORAGE,
    "chuva": PRECIP, "precipitacao": PRECIP,
}


@dataclass(frozen=True)
class Observation:
    """Un valor, de un sitio, en un instante."""

    source: str          # copel_mhb | ana | inpe_merge | ons
    site: str            # clave del sitio en la configuración
    variable: str        # una de las constantes de arriba
    timestamp: datetime  # siempre en UTC
    value: float
    unit: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError(f"timestamp sin zona horaria: {self.timestamp!r}")
        if not self.unit:
            object.__setattr__(self, "unit", UNITS.get(self.variable, ""))

    def as_row(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "site": self.site,
            "variable": self.variable,
            "timestamp": self.timestamp.astimezone(timezone.utc),
            "value": self.value,
            "unit": self.unit,
        }


@dataclass
class ProbeResult:
    """Lo que una fuente devolvió realmente, sin interpretarlo.

    El punto de `probe` es poder arreglar el parseo mirando datos reales en
    vez de adivinar: reporta el crudo y lo que se logró extraer de él.
    """

    source: str
    ok: bool
    url: str = ""
    http_status: int | None = None
    content_type: str = ""
    size_bytes: int | None = None
    parsed: int = 0
    notes: list[str] = field(default_factory=list)
    sample: str = ""
    error: str = ""

    def render(self) -> str:
        head = "OK " if self.ok else "FALLA"
        lines = [f"[{head}] {self.source}"]
        if self.url:
            lines.append(f"  url          {self.url}")
        if self.http_status is not None:
            lines.append(f"  http         {self.http_status}")
        if self.content_type:
            lines.append(f"  content-type {self.content_type}")
        if self.size_bytes is not None:
            lines.append(f"  tamaño       {self.size_bytes:,} bytes")
        lines.append(f"  observaciones extraídas: {self.parsed}")
        for note in self.notes:
            lines.append(f"  · {note}")
        if self.error:
            lines.append(f"  ERROR: {self.error}")
        if self.sample:
            lines.append("  --- muestra del crudo ---")
            lines.extend(f"  {ln}" for ln in self.sample.splitlines()[:40])
        return "\n".join(lines)


def normalize_label(text: str) -> str:
    """Minúsculas, sin acentos ni puntuación: para casar etiquetas."""
    import re
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", stripped.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def match_variable(label: str) -> str | None:
    """Mapea una etiqueta de columna a una variable normalizada."""
    norm = normalize_label(label)
    if not norm:
        return None
    if norm in VARIABLE_ALIASES:
        return VARIABLE_ALIASES[norm]
    # Coincidencia por la clave más larga contenida en la etiqueta, para que
    # "vazao defluente (m3/s)" gane con "vazao defluente" y no con "vazao".
    best: tuple[int, str] | None = None
    for alias, var in VARIABLE_ALIASES.items():
        if alias in norm and (best is None or len(alias) > best[0]):
            best = (len(alias), var)
    return best[1] if best else None
