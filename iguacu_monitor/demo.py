"""Datos de ejemplo para ver el panel sin acceso a las fuentes.

Los valores actuales vienen del tablero operativo FEWS-Yacyretá del
23-oct-2026 a las 22:00 (hora local, UTC-3). Las series de 48 h son
SINTÉTICAS: terminan en el valor actual con la tendencia que mostraba el
tablero, pero la historia anterior es inventada para dibujar la sparkline.
Sirven para revisar el diseño, nunca para leer la cuenca.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from iguacu_monitor.model import (LEVEL, PRECIP, QAFL, QDEF, QOBS, QTUR, QVER,
                                  STORAGE, Observation)

# 23-oct-2026 22:00 en UTC-3.
SNAPSHOT = datetime(2026, 10, 24, 1, 0, tzinfo=timezone.utc)

NOTE = ("Valores del tablero del 23-oct 22:00; "
        "las series de 48 h son sintéticas.")

# key: (sale, tendencia/h, entra, vierte, turbina, nivel m, volumen %)
PLANTS = {
    "foz_do_areia":   (3085,  32, 2261, 1666, 1419, 737.6, 85),
    "segredo":        (4026,  11, 3982, 2700, 1326, 605.9, 77),
    "salto_santiago": (5467,  16, 5467, 3946, 1520, 500.6, 74),
    "salto_osorio":   (6045,  27, 5892, 4246, 1799, 396.4, 92),
    "salto_caxias":   (7844, -71, 7463, 5653, 2191, 323.9, 45),
    "baixo_iguacu":   (8892, 137, 8439, 7016, 1875, 258.4, 42),
}

# key: (caudal, tendencia/h)
STATIONS = {
    "porto_capanema":  (8978, 258),
    "hotel_cataratas": (7253, 8),
}

# Lámina diaria sintética que suma los 55,9 mm "desde 21-oct" del tablero.
RAIN_MM = {21: 31.4, 22: 18.2, 23: 6.3}


def _synthetic_series(current: float, trend: float, seed: int,
                      hours: int = 48) -> list[tuple[int, float]]:
    """(horas atrás, valor). Las últimas 3 h son exactamente lineales, así la
    tendencia calculada coincide con la del tablero."""
    rng = random.Random(seed)
    out = []
    for h in range(hours, -1, -1):
        if h <= 3:
            value = current - trend * h
        else:
            k = (h - 3) / (hours - 3)
            drift = -trend * 3 - (h - 3) * trend * 0.25
            wave = 0.07 * current * math.sin((h - 3) / 6.5 + seed) * k
            noise = rng.uniform(-0.012, 0.012) * current * k
            value = current + drift + wave + noise
        out.append((h, max(value, 0.0)))
    return out


def example_observations(now: datetime = SNAPSHOT) -> list[Observation]:
    obs: list[Observation] = []

    def add(source, site, var, value, when=now):
        obs.append(Observation(source=source, site=site, variable=var,
                               timestamp=when, value=float(value),
                               meta={"demo": True}))

    for seed, (key, row) in enumerate(PLANTS.items()):
        sale, trend, entra, vierte, turbina, nivel, vol = row
        for h, value in _synthetic_series(sale, trend, seed):
            add("demo", key, QDEF, value, now - timedelta(hours=h))
        add("demo", key, QAFL, entra)
        add("demo", key, QVER, vierte)
        add("demo", key, QTUR, turbina)
        add("demo", key, LEVEL, nivel)
        add("demo", key, STORAGE, vol)

    for seed, (key, (q, trend)) in enumerate(STATIONS.items(), start=10):
        # El tablero marca el máximo de la ventana en el dato actual. Lo que
        # la serie sintética pasa por encima se refleja por debajo: recortar
        # dejaría una meseta plana que no se parece a ningún hidrograma.
        for h, value in _synthetic_series(q, trend, seed):
            add("demo", key, QOBS, q - abs(value - q), now - timedelta(hours=h))

    for day, mm in RAIN_MM.items():
        add("demo", "basin", PRECIP, mm,
            datetime(2026, 10, day, 12, 0, tzinfo=timezone.utc))
    return obs
