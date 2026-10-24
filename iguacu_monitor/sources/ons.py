"""ONS — datos hidráulicos por reservatório (respaldo de regulación).

Respaldo y complemento de COPEL: cubre toda la cascada, incluidas las usinas
que COPEL no opera (Salto Santiago, Salto Osório, Baixo Iguaçu). Los datos
son consolidados y llegan con rezago, así que sirven para contexto y control
de calidad, no para la lectura del minuto.

Se descubren los recursos vía la API CKAN del portal en vez de fijar la URL
del bucket S3, que cambia entre conjuntos.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from io import BytesIO

from iguacu_monitor import http
from iguacu_monitor.model import QAFL, QDEF, Observation, ProbeResult, normalize_label
from iguacu_monitor.sources.base import Source

def _norm_col(name: str) -> str:
    """Clave canónica de una columna: sólo letras y dígitos, en minúsculas.

    El ONS alterna entre `din_instante`, `Din Instante` y `DIN_INSTANTE`
    según el recurso, así que comparamos sobre una forma sin separadores.
    """
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


# Nombres de columna del ONS -> variable normalizada.
COLUMN_MAP = {
    "val_vazaoafluente": QAFL,
    "val_vazaodefluente": QDEF,
    "val_vazaoafluentemedia": QAFL,
    "val_vazaodefluentemedia": QDEF,
}
NAME_COLUMNS = ("nom_reservatorio", "nom_usina", "nom_ree")
DATE_COLUMNS = ("din_instante", "ear_data", "din_data")


class ONS(Source):
    name = "ons"

    def _resources(self) -> list[dict]:
        resp = http.get(
            self.opts.get("ckan", "https://dados.ons.org.br/api/3/action/package_show"),
            params={"id": self.opts.get("dataset", "dados-hidrologicos-res")},
            timeout=self.opts.get("timeout", 180),
        )
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("success"):
            raise http.FetchError(f"CKAN rechazó la consulta: {payload}")
        return payload["result"].get("resources", [])

    def resource_for_year(self, year: int) -> dict | None:
        """Elige el recurso Parquet del año pedido."""
        best: dict | None = None
        for res in self._resources():
            fmt = (res.get("format") or "").upper()
            url = res.get("url") or ""
            if fmt != "PARQUET" or str(year) not in url:
                continue
            # Preferir la base diaria consolidada sobre la horaria cruda.
            if best is None or "_di" in url or "DIARIO" in url.upper():
                best = res
        return best

    def _load(self, url: str):
        import pandas as pd

        resp = http.get(url, timeout=self.opts.get("timeout", 180))
        resp.raise_for_status()
        return pd.read_parquet(BytesIO(resp.content))

    def _parse(self, frame, start: date, end: date) -> tuple[list[Observation], list[str]]:
        notes: list[str] = []
        lowered = {_norm_col(c): c for c in frame.columns}

        name_col = next((lowered[_norm_col(c)] for c in NAME_COLUMNS
                         if _norm_col(c) in lowered), None)
        date_col = next((lowered[_norm_col(c)] for c in DATE_COLUMNS
                         if _norm_col(c) in lowered), None)
        if not name_col or not date_col:
            notes.append(f"columnas inesperadas: {list(frame.columns)[:15]}")
            return [], notes

        value_cols = {lowered[_norm_col(c)]: v for c, v in COLUMN_MAP.items()
                      if _norm_col(c) in lowered}
        if not value_cols:
            notes.append(f"sin columnas de caudal reconocidas en: {list(frame.columns)[:15]}")
            return [], notes

        import pandas as pd

        wanted = {}
        for plant in self.cfg.plants:
            if plant.ons_label:
                wanted[normalize_label(plant.ons_label)] = plant.key

        stamps = pd.to_datetime(frame[date_col], errors="coerce")
        mask = (stamps.dt.date >= start) & (stamps.dt.date <= end)
        frame = frame.loc[mask]
        stamps = stamps.loc[mask]

        out: list[Observation] = []
        matched: set[str] = set()
        for idx, row in frame.iterrows():
            label = normalize_label(str(row[name_col]))
            key = next((k for lbl, k in wanted.items() if lbl and lbl in label), None)
            if key is None:
                continue
            when = stamps.loc[idx]
            if pd.isna(when):
                continue
            when = when.to_pydatetime()
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            for col, var in value_cols.items():
                value = row[col]
                if pd.isna(value):
                    continue
                out.append(Observation(
                    source=self.name, site=key, variable=var,
                    timestamp=when, value=float(value),
                ))
                matched.add(key)

        missing = sorted({p.key for p in self.cfg.plants if p.ons_label} - matched)
        if missing:
            notes.append(f"sin datos ONS para: {', '.join(missing)} (revisar ons_label)")
        return out, notes

    def probe(self) -> ProbeResult:
        result = ProbeResult(source=self.name, ok=False, url=self.opts.get("ckan", ""))
        try:
            resources = self._resources()
        except Exception as exc:  # noqa: BLE001
            result.error = str(exc)
            return result

        result.http_status = 200
        parquet = [r for r in resources if (r.get("format") or "").upper() == "PARQUET"]
        result.notes.append(f"{len(resources)} recursos; {len(parquet)} en Parquet")

        res = self.resource_for_year(date.today().year)
        if res is None:
            result.error = f"no hay recurso Parquet para {date.today().year}"
            result.sample = "\n".join(
                f"{r.get('format','?'):8} {r.get('url','')}" for r in resources[:10])
            return result

        result.url = res.get("url", "")
        try:
            frame = self._load(result.url)
        except Exception as exc:  # noqa: BLE001
            result.error = f"no se pudo leer el Parquet: {exc}"
            return result

        result.size_bytes = int(frame.memory_usage(deep=True).sum())
        result.notes.append(f"{len(frame):,} filas, columnas: {list(frame.columns)[:12]}")

        end = date.today()
        start = end.replace(day=1)
        obs, notes = self._parse(frame, start, end)
        result.parsed = len(obs)
        result.notes.extend(notes)
        result.ok = True
        result.sample = frame.head(3).to_string()[:1200]
        return result

    def fetch(self, start: date, end: date) -> list[Observation]:
        out: list[Observation] = []
        for year in range(start.year, end.year + 1):
            res = self.resource_for_year(year)
            if res is None:
                continue
            frame = self._load(res["url"])
            obs, _ = self._parse(frame, start, end)
            out.extend(obs)
        return out
