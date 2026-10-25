"""ONS — datos hidráulicos por reservatório.

Cubre toda la cascada del Iguazú, incluidas las usinas que COPEL no opera
(Salto Santiago, Salto Osório, Baixo Iguaçu), con afluente, turbinado,
vertido, defluente, nivel aguas arriba y volumen útil.

Dos bases:

* `hourly` (por defecto): `dados_hidrologicos_ho`, un Parquet por mes, no
  consistida, con 1 a 2 h de rezago. Es la que sirve para monitorear.
* `daily`: `dados-hidrologicos-res`, un Parquet por año, consolidada y con
  uno o dos días de rezago.

Los recursos se descubren vía la API CKAN del portal en vez de fijar la URL
del bucket S3. Las usinas se identifican por `id_reservatorio` (IGSCAX,
IGGBMU…), que es estable; el nombre sólo se usa como respaldo, porque ONS
los abrevia ("G. B. MUNHOZ") y los rellena con espacios.

ONS publica en hora de Brasilia (UTC-3) sin declararlo.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from io import BytesIO

from iguacu_monitor import http
from iguacu_monitor.model import (LEVEL, QAFL, QDEF, QTUR, QVER, STORAGE,
                                  Observation, ProbeResult, normalize_label)
from iguacu_monitor.sources.base import Source

BRASILIA = timezone(timedelta(hours=-3))


def _norm_col(name: str) -> str:
    """Clave canónica de una columna: sólo letras y dígitos, en minúsculas.

    El ONS alterna entre `din_instante`, `Din Instante` y `DIN_INSTANTE`
    según el recurso, así que comparamos sobre una forma sin separadores.
    """
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


# Columna del ONS -> variable normalizada. La base diaria trae
# `val_volumeutilcon` y la horaria `val_volumeutil`; ambas en %.
COLUMN_MAP = {
    "val_vazaoafluente": QAFL,
    "val_vazaodefluente": QDEF,
    "val_vazaoturbinada": QTUR,
    "val_vazaovertida": QVER,
    "val_nivelmontante": LEVEL,
    "val_volumeutil": STORAGE,
    "val_volumeutilcon": STORAGE,
}
ID_COLUMNS = ("id_reservatorio",)
NAME_COLUMNS = ("nom_reservatorio", "nom_usina")
DATE_COLUMNS = ("din_instante", "ear_data", "din_data")


def _months(start: date, end: date) -> list[tuple[int, int]]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


class ONS(Source):
    name = "ons"

    @property
    def hourly(self) -> bool:
        return self.opts.get("mode", "hourly") == "hourly"

    def _resources(self, dataset: str) -> list[dict]:
        resp = http.get(
            self.opts.get("ckan", "https://dados.ons.org.br/api/3/action/package_show"),
            params={"id": dataset},
            timeout=self.opts.get("timeout", 180),
        )
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("success"):
            raise http.FetchError(f"CKAN rechazó la consulta de {dataset}: {payload}")
        return payload["result"].get("resources", [])

    def resource_urls(self, start: date, end: date) -> list[str]:
        """URLs Parquet que cubren [start, end] en la base configurada."""
        if self.hourly:
            dataset = self.opts.get("dataset_hourly", "dados_hidrologicos_ho")
            keys = [f"{y}_{m:02d}" for y, m in _months(start, end)]
        else:
            dataset = self.opts.get("dataset", "dados-hidrologicos-res")
            keys = [str(y) for y in range(start.year, end.year + 1)]
        parquet = [r.get("url", "") for r in self._resources(dataset)
                   if (r.get("format") or "").upper() == "PARQUET"]
        urls: list[str] = []
        for key in keys:
            match = next((u for u in parquet if u.rsplit("/", 1)[-1].endswith(f"{key}.parquet")), None)
            if match and match not in urls:
                urls.append(match)
        return urls

    def _load(self, url: str):
        import pandas as pd

        resp = http.get(url, timeout=self.opts.get("timeout", 180))
        resp.raise_for_status()
        return pd.read_parquet(BytesIO(resp.content))

    def _parse(self, frame, start: date, end: date) -> tuple[list[Observation], list[str]]:
        import pandas as pd

        notes: list[str] = []
        lowered = {_norm_col(c): c for c in frame.columns}

        def pick(candidates):
            return next((lowered[_norm_col(c)] for c in candidates if _norm_col(c) in lowered), None)

        id_col, name_col, date_col = pick(ID_COLUMNS), pick(NAME_COLUMNS), pick(DATE_COLUMNS)
        if not (id_col or name_col) or not date_col:
            notes.append(f"columnas inesperadas: {list(frame.columns)[:15]}")
            return [], notes

        value_cols = {lowered[_norm_col(c)]: v for c, v in COLUMN_MAP.items()
                      if _norm_col(c) in lowered}
        if not any(v in (QAFL, QDEF) for v in value_cols.values()):
            notes.append(f"sin columnas de caudal reconocidas en: {list(frame.columns)[:15]}")
            return [], notes

        by_id = {p.ons_id.strip().upper(): p.key for p in self.cfg.plants if p.ons_id}
        by_name = {normalize_label(p.ons_label): p.key for p in self.cfg.plants if p.ons_label}

        keys = pd.Series([None] * len(frame), index=frame.index, dtype=object)
        if id_col:
            keys = frame[id_col].astype(str).str.strip().str.upper().map(by_id)
        if name_col:
            # Respaldo por nombre exacto (normalizado), nunca por subcadena:
            # "SEGREDO" no debe casar con un futuro "SEGREDO II".
            fallback = frame[name_col].astype(str).map(lambda n: by_name.get(normalize_label(n)))
            keys = keys.fillna(fallback)

        frame = frame.assign(_key=keys).dropna(subset=["_key"])
        stamps = pd.to_datetime(frame[date_col], errors="coerce")
        mask = stamps.notna() & (stamps.dt.date >= start) & (stamps.dt.date <= end)
        frame, stamps = frame.loc[mask], stamps.loc[mask]

        out: list[Observation] = []
        for idx, row in frame.iterrows():
            when = stamps.loc[idx].to_pydatetime()
            when = (when.replace(tzinfo=BRASILIA) if when.tzinfo is None else when).astimezone(timezone.utc)
            for col, var in value_cols.items():
                value = row[col]
                if pd.isna(value):
                    continue
                out.append(Observation(source=self.name, site=row["_key"], variable=var,
                                       timestamp=when, value=float(value)))

        matched = {o.site for o in out}
        missing = sorted({p.key for p in self.cfg.plants if p.ons_id or p.ons_label} - matched)
        if missing:
            notes.append(f"sin datos ONS para: {', '.join(missing)} (revisar ons_id)")
        return out, notes

    def probe(self) -> ProbeResult:
        result = ProbeResult(source=self.name, ok=False, url=self.opts.get("ckan", ""))
        end = date.today()
        start = end - timedelta(days=2)
        try:
            urls = self.resource_urls(start, end)
        except Exception as exc:  # noqa: BLE001
            result.error = str(exc)
            return result
        result.http_status = 200
        result.notes.append(f"base {'horaria' if self.hourly else 'diaria'}: {len(urls)} archivo(s)")
        if not urls:
            result.error = "no hay recurso Parquet para el período"
            return result

        result.url = urls[-1]
        try:
            frame = self._load(result.url)
        except Exception as exc:  # noqa: BLE001
            result.error = f"no se pudo leer el Parquet: {exc}"
            return result

        result.size_bytes = int(frame.memory_usage(deep=True).sum())
        obs, notes = self._parse(frame, start, end)
        result.parsed = len(obs)
        result.notes.extend(notes)
        result.ok = bool(obs)
        if obs:
            latest: dict[str, datetime] = {}
            for o in obs:
                latest[o.site] = max(latest.get(o.site, o.timestamp), o.timestamp)
            result.notes.extend(
                f"{k}: último dato {v.astimezone(BRASILIA):%d/%m %H:%M} (hora de Brasilia)"
                for k, v in sorted(latest.items(), key=lambda kv: kv[1]))
        else:
            result.error = "no se extrajo ninguna observación"
            result.sample = frame.head(3).to_string()[:1200]
        return result

    def fetch(self, start: date, end: date) -> list[Observation]:
        out: list[Observation] = []
        for url in self.resource_urls(start, end):
            obs, _ = self._parse(self._load(url), start, end)
            out.extend(obs)
        return out
