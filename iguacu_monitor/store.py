"""Persistencia local de observaciones en Parquet.

Un monitor que corre periódicamente necesita acumular historia sin duplicar
lo que ya tiene: la clave natural es (source, site, variable, timestamp).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from iguacu_monitor.model import Observation

KEY = ["source", "site", "variable", "timestamp"]


def _frame(observations: Iterable[Observation]):
    import pandas as pd

    rows = [o.as_row() for o in observations]
    if not rows:
        return pd.DataFrame(columns=[*KEY, "value", "unit"])
    frame = pd.DataFrame(rows)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame


def save(observations: Sequence[Observation], path: str | Path) -> int:
    """Fusiona observaciones nuevas con las guardadas. Devuelve cuántas se agregaron."""
    import pandas as pd

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = _frame(observations)
    if fresh.empty:
        return 0

    if path.exists():
        existing = pd.read_parquet(path)
        existing["timestamp"] = pd.to_datetime(existing["timestamp"], utc=True)
        before = len(existing)
        merged = pd.concat([existing, fresh], ignore_index=True)
    else:
        before = 0
        merged = fresh

    # La lectura más reciente de una misma clave gana: las fuentes corrigen
    # valores provisorios al consolidarlos.
    merged = merged.drop_duplicates(subset=KEY, keep="last")
    merged = merged.sort_values(KEY).reset_index(drop=True)
    merged.to_parquet(path, index=False)
    return len(merged) - before


def load(path: str | Path):
    import pandas as pd

    path = Path(path)
    if not path.exists():
        return pd.DataFrame(columns=[*KEY, "value", "unit"])
    frame = pd.read_parquet(path)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame
