"""Interfaz común a todas las fuentes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from iguacu_monitor.config import Config
from iguacu_monitor.model import Observation, ProbeResult


class Source(ABC):
    """Una fuente de datos.

    `probe` existe para diagnosticar sin suponer: baja el crudo y reporta qué
    llegó y cuánto se pudo interpretar. `fetch` es el camino de producción.
    """

    name: str = "base"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.opts = cfg.source(self.name)

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled(self.name)

    @abstractmethod
    def probe(self) -> ProbeResult:
        """Diagnóstico: qué devuelve la fuente ahora mismo."""

    @abstractmethod
    def fetch(self, start: date, end: date) -> list[Observation]:
        """Observaciones en [start, end], ambos inclusive."""
