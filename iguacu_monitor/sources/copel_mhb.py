"""COPEL — Monitoramento Hidrológico de Bacias (MHB), cuenca del Iguazú.

Cubre a la vez la regulación de la cascada (afluente, turbinada, vertida,
defluente por usina) y las estaciones hidrométricas que COPEL opera, entre
ellas Porto Capanema y Hotel Cataratas. Publica la hora actual más siete
días de historia.

ADVERTENCIA DE PROCEDENCIA: la estructura del DOM no pudo verificarse contra
la página real (el entorno donde se escribió esto no tiene salida a
copel.com). El parseo es deliberadamente tolerante y `probe` vuelca las
tablas encontradas para poder ajustarlo contra la página real.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from iguacu_monitor import http, tables
from iguacu_monitor.model import Observation, ProbeResult
from iguacu_monitor.sources.base import Source


class CopelMHB(Source):
    name = "copel_mhb"

    @property
    def url(self) -> str:
        return self.opts.get("url", "https://www.copel.com/mhbweb/paginas/bacia-iguacu.jsf")

    def _read_tables(self, html: str) -> list:
        return tables.read_tables(html)

    def _parse(self, html: str, now: datetime) -> tuple[list[Observation], list[str]]:
        frames = self._read_tables(html)
        if not frames:
            return [], ["el HTML no contiene tablas: la página probablemente "
                        "carga los datos por JavaScript; ver nota en probe"]

        obs: list[Observation] = []
        notes: list[str] = [f"{len(frames)} tabla(s) en el HTML"]

        # Las usinas y las estaciones viven en tablas distintas, pero ambas
        # se casan por `copel_label`, así que una sola pasada alcanza.
        got, sub_notes = tables.extract(
            frames, self.cfg.sites, source=self.name,
            timestamp=now, label_attr="copel_label",
        )
        obs.extend(got)
        notes.extend(sub_notes)
        return obs, notes

    def probe(self) -> ProbeResult:
        result = ProbeResult(source=self.name, ok=False, url=self.url)
        try:
            resp = http.get(self.url, timeout=self.opts.get("timeout", 60))
        except http.FetchError as exc:
            result.error = str(exc)
            return result

        result.http_status = resp.status_code
        result.content_type = resp.headers.get("Content-Type", "")
        result.size_bytes = len(resp.content)
        if resp.status_code != 200:
            result.error = f"HTTP {resp.status_code}"
            result.sample = resp.text[:1500]
            return result

        now = datetime.now(timezone.utc)
        obs, notes = self._parse(resp.text, now)
        result.parsed = len(obs)
        result.notes = notes
        result.ok = True

        # Volcar los encabezados reales: es lo que hace falta para ajustar
        # el mapeo si alguna columna no fue reconocida.
        frames = self._read_tables(resp.text)
        head: list[str] = []
        for i, frame in enumerate(frames[:6]):
            head.append(f"tabla {i}: {list(frame.columns)[:8]}")
            head.append(f"  primera fila: {frame.head(1).to_dict('records')}")
        result.sample = "\n".join(head) if head else resp.text[:1500]
        return result

    def fetch(self, start: date, end: date) -> list[Observation]:
        """Lee el estado publicado ahora.

        La página expone el instante actual y una ventana corta de historia;
        no acepta un rango arbitrario. Se ignoran `start`/`end` y se sella la
        lectura con la hora de consulta, que es lo que un monitor necesita.
        """
        resp = http.get(self.url, timeout=self.opts.get("timeout", 60))
        resp.raise_for_status()
        obs, _ = self._parse(resp.text, datetime.now(timezone.utc))
        return obs
