"""CEMADEN — pluviómetros automáticos municipales, acumulado de 24 h.

El mapa interactivo de CEMADEN (mapainterativo.cemaden.gov.br) lee un JSONP
abierto con todas las estaciones del país:

    https://resources.cemaden.gov.br/dados/311_24.json
    estacoes([{"atualizado": "2026-10-25 12:48:43 UTC",
               "estacao": [{"codestacao": "410680301A", "acumulado": 104.8,
                            "latitude": ..., "longitude": ..., ...}, ...]}])

Sólo publica el acumulado móvil de 24 h hasta `atualizado` (no hay 1, 6 ni
72 h, ni series): cada consulta guarda una foto. `acumulado` nulo es una
estación sin dato. No pasa por ANA, así que sigue midiendo cuando ANA se cae.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from iguacu_monitor import http
from iguacu_monitor.model import PRECIP_24H, Observation, ProbeResult
from iguacu_monitor.sources.base import Source

DEFAULT_URL = "https://resources.cemaden.gov.br/dados/311_24.json"


def parse(text: str) -> tuple[datetime | None, list[dict]]:
    """(instante de actualización en UTC, estaciones) a partir del JSONP."""
    body = text[text.index("(") + 1: text.rindex(")")] if "(" in text else text
    blocks = json.loads(body)
    stamp, stations = None, []
    for block in blocks:
        raw = str(block.get("atualizado") or "").replace(" UTC", "")
        if raw:
            stamp = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        stations.extend(block.get("estacao") or [])
    return stamp, stations


class Cemaden(Source):
    name = "cemaden"

    @property
    def url(self) -> str:
        return self.opts.get("url", DEFAULT_URL)

    def _get(self) -> tuple[datetime | None, list[dict]]:
        resp = http.get(self.url, timeout=self.opts.get("timeout", 60))
        resp.raise_for_status()
        return parse(resp.text)

    def _observations(self, stamp: datetime | None, stations: list[dict]) -> list[Observation]:
        if stamp is None:
            return []
        known = {str(g.code) for g in self.cfg.gauges if g.network == "cemaden"}
        out = []
        for st in stations:
            code = str(st.get("codestacao") or "")
            value = st.get("acumulado")
            if code in known and value is not None:
                out.append(Observation(source=self.name, site=f"cem_{code}", variable=PRECIP_24H,
                                       timestamp=stamp, value=float(value), unit="mm"))
        return out

    def fetch(self, start: date, end: date) -> list[Observation]:
        """La foto actual: el acumulado de 24 h no acepta otro rango."""
        return self._observations(*self._get())

    def probe(self) -> ProbeResult:
        result = ProbeResult(source=self.name, ok=False, url=self.url)
        try:
            stamp, stations = self._get()
        except (http.FetchError, ValueError) as exc:
            result.error = str(exc)
            return result
        obs = self._observations(stamp, stations)
        result.ok = bool(obs)
        result.parsed = len(obs)
        result.notes = [f"{len(stations)} estaciones en el país, actualizado {stamp:%Y-%m-%d %H:%M} UTC"
                        if stamp else "sin campo 'atualizado'"]
        if not obs:
            result.error = "ninguna estación del catálogo con dato"
        return result

    def discover(self, area) -> list[dict]:
        """Estaciones dentro de `area` (geometría shapely), para el catálogo."""
        from shapely.geometry import Point

        _, stations = self._get()
        found = []
        for st in stations:
            lat, lon = st.get("latitude"), st.get("longitude")
            if lat is None or lon is None or not area.contains(Point(lon, lat)):
                continue
            found.append({
                "code": str(st["codestacao"]),
                "name": f"{st.get('cidade', '')} - {st.get('nomeestacao', '')}".strip(" -"),
                "operator": st.get("sigla") or "CEMADEN",
                "lat": round(float(lat), 5), "lon": round(float(lon), 5),
                "type": st.get("tipoestacao", ""),
            })
        return sorted(found, key=lambda g: g["code"])
