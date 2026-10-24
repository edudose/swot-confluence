"""Extracción tolerante de observaciones desde tablas HTML.

La página MHB de COPEL es JSF: las etiquetas de columna y el orden de las
filas pueden cambiar entre versiones. En vez de fijar selectores —que se
rompen en silencio y devuelven datos vacíos sin avisar— buscamos las filas
por nombre de sitio y las columnas por nombre de variable, y reportamos lo
que no se pudo casar.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, Sequence

from iguacu_monitor.config import Site
from iguacu_monitor.model import Observation, match_variable, normalize_label

# "1.234,5" (portugués) y "1,234.5" (inglés) conviven en estos portales.
_NUM = re.compile(r"-?\d[\d.,\s]*")


def parse_number(text: str) -> float | None:
    """Convierte un número en formato brasileño o inglés a float."""
    if text is None:
        return None
    raw = str(text).strip()
    if not raw or raw.lower() in {"-", "--", "n/a", "nan", "none", "s/d"}:
        return None
    m = _NUM.search(raw.replace("\xa0", " "))
    if not m:
        return None
    token = m.group(0).strip().replace(" ", "")

    has_dot, has_comma = "." in token, "," in token
    if has_dot and has_comma:
        # El separador decimal es el que aparece más a la derecha.
        if token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif has_comma:
        # Una sola coma con 1-2 decimales es decimal; si no, es de miles.
        token = token.replace(",", ".") if re.search(r",\d{1,2}$", token) else token.replace(",", "")
    elif has_dot:
        if not re.search(r"\.\d{1,2}$", token) and re.search(r"\.\d{3}", token):
            token = token.replace(".", "")
    try:
        return float(token)
    except ValueError:
        return None


def _site_for_row(row_label: str, sites: Sequence[Site], attr: str) -> Site | None:
    """Casa el texto de una fila con un sitio por su etiqueta configurada."""
    norm_row = normalize_label(row_label)
    if not norm_row:
        return None
    best: tuple[int, Site] | None = None
    for site in sites:
        label = getattr(site, attr, None)
        if not label:
            continue
        norm_label = normalize_label(label)
        if norm_label and norm_label in norm_row:
            if best is None or len(norm_label) > best[0]:
                best = (len(norm_label), site)
    return best[1] if best else None


def extract(
    tables: Iterable["object"],
    sites: Sequence[Site],
    *,
    source: str,
    timestamp: datetime,
    label_attr: str = "copel_label",
) -> tuple[list[Observation], list[str]]:
    """Extrae observaciones de una lista de DataFrames.

    Devuelve (observaciones, notas). Las notas describen lo que quedó sin
    casar, que es la información que hace falta para corregir el parseo.
    """
    import pandas as pd

    observations: list[Observation] = []
    notes: list[str] = []
    seen_sites: set[str] = set()

    for idx, table in enumerate(tables):
        if not isinstance(table, pd.DataFrame) or table.empty:
            continue

        # Mapear columna -> variable usando el encabezado.
        col_vars: dict[object, str] = {}
        for col in table.columns:
            header = " ".join(str(p) for p in col) if isinstance(col, tuple) else str(col)
            var = match_variable(header)
            if var:
                col_vars[col] = var
        if not col_vars:
            continue

        for _, row in table.iterrows():
            # El nombre del sitio suele estar en las primeras celdas.
            row_text = " ".join(str(row[c]) for c in list(table.columns)[:3])
            site = _site_for_row(row_text, sites, label_attr)
            if site is None:
                continue
            for col, var in col_vars.items():
                value = parse_number(row[col])
                if value is None:
                    continue
                observations.append(Observation(
                    source=source, site=site.key, variable=var,
                    timestamp=timestamp, value=value,
                    meta={"table": idx},
                ))
                seen_sites.add(site.key)

    expected = {s.key for s in sites if getattr(s, label_attr, None)}
    missing = sorted(expected - seen_sites)
    if missing:
        notes.append(f"sin datos para: {', '.join(missing)} (revisar etiqueta {label_attr})")
    return observations, notes


def read_tables(html: str) -> list["object"]:
    """Convierte las tablas de un HTML en DataFrames de puro texto.

    No usamos `pandas.read_html` a propósito: aplica `thousands=","` y
    `decimal="."` por defecto, lo que convierte un nivel de "741,20" m en el
    entero 74120 y una vazão de "10.030" m3/s en 10,03 — sin error ni aviso.
    En datos brasileños esa inferencia corrompe justo los valores que el
    monitor tiene que leer bien, así que dejamos todas las celdas como texto
    y que `parse_number`, que conoce ambos formatos, decida.
    """
    import pandas as pd
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    frames: list[pd.DataFrame] = []

    for table in soup.find_all("table"):
        rows: list[list[str]] = []
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"], recursive=False) or tr.find_all(["th", "td"])
            if not cells:
                continue
            rows.append([c.get_text(" ", strip=True) for c in cells])
        if not rows:
            continue

        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]

        # Encabezado: la primera fila, si la tabla declara <th>.
        if table.find("th") and len(rows) > 1:
            header, body = rows[0], rows[1:]
        else:
            header, body = [f"col{i}" for i in range(width)], rows

        # Desambiguar encabezados repetidos o vacíos.
        seen: dict[str, int] = {}
        columns: list[str] = []
        for i, name in enumerate(header):
            name = name or f"col{i}"
            if name in seen:
                seen[name] += 1
                name = f"{name}.{seen[name]}"
            else:
                seen[name] = 0
            columns.append(name)

        frames.append(pd.DataFrame(body, columns=columns, dtype=str))

    return frames
