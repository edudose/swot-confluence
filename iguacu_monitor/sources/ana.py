"""ANA — caudal observado en estaciones telemétricas.

Dos caminos:

* `legacy`  -> telemetriaws1.ana.gov.br/ServiceANA.asmx, XML, sin credenciales.
* `hidrowebservice` -> API nueva, requiere usuario y contraseña que ANA
  entrega por correo (hidro@ana.gov.br / telemetria@ana.gov.br). El token se
  pide en /EstacoesTelemetricas/OAUth/v1 y vence; se renueva solo.

`discover` resuelve el código de una estación por nombre, que es como se
completa `ana_code` cuando no se conoce (caso Porto Capanema).
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

from iguacu_monitor import http
from iguacu_monitor.model import LEVEL, PRECIP, QOBS, Observation, ProbeResult
from iguacu_monitor.sources.base import Source

# Los datos de ANA vienen en hora local de Brasilia (UTC-3), sin indicarlo.
BRASILIA = timezone(timedelta(hours=-3))

# El esquema de ANA trae el typo "Hidrometereologicos"; casamos por sufijo
# para sobrevivir a que lo corrijan.
_FIELD = re.compile(r"\{.*\}|^")


def _text(node: ET.Element, *names: str) -> str | None:
    for child in node:
        tag = child.tag.rsplit("}", 1)[-1].lower()
        if tag in names:
            return (child.text or "").strip() or None
    return None


def _to_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return None


class ANA(Source):
    name = "ana"

    # ---------------------------------------------------------------- legacy
    @property
    def legacy_base(self) -> str:
        return self.opts.get("legacy_base", "https://telemetriaws1.ana.gov.br/ServiceANA.asmx")

    def _legacy_series(self, code: int, start: date, end: date) -> list[ET.Element]:
        resp = http.get(
            f"{self.legacy_base}/DadosHidrometeorologicos",
            params={
                "codEstacao": str(code),
                "dataInicio": start.strftime("%d/%m/%Y"),
                "dataFim": end.strftime("%d/%m/%Y"),
            },
            timeout=self.opts.get("timeout", 90),
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        return [n for n in root.iter() if n.tag.rsplit("}", 1)[-1].lower().startswith("dadoshidromet")]

    def _parse_records(self, nodes: list[ET.Element], site_key: str) -> list[Observation]:
        out: list[Observation] = []
        for node in nodes:
            stamp = _text(node, "datahora", "data_hora")
            if not stamp:
                continue
            when: datetime | None = None
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
                try:
                    when = datetime.strptime(stamp[:19], fmt)
                    break
                except ValueError:
                    continue
            if when is None:
                continue
            when = when.replace(tzinfo=BRASILIA).astimezone(timezone.utc)

            # La telemetría de ANA entrega el nivel en centímetros.
            for names, var, scale in (
                (("vazao",), QOBS, 1.0),
                (("nivel",), LEVEL, 0.01),
                (("chuva",), PRECIP, 1.0),
            ):
                value = _to_float(_text(node, *names))
                if value is not None:
                    value *= scale
                    out.append(Observation(
                        source=self.name, site=site_key, variable=var,
                        timestamp=when, value=value,
                    ))
        return out

    # -------------------------------------------------------- hidrowebservice
    def _hws_token(self) -> str:
        user, password = os.getenv("ANA_HWS_USER"), os.getenv("ANA_HWS_PASS")
        if not user or not password:
            raise http.FetchError(
                "faltan credenciales: exportá ANA_HWS_USER y ANA_HWS_PASS. "
                "Se piden por correo a hidro@ana.gov.br o telemetria@ana.gov.br."
            )
        base = self.opts.get("hws_base", "https://www.ana.gov.br/hidrowebservice")
        resp = http.get(
            f"{base}/EstacoesTelemetricas/OAUth/v1",
            headers={"Identificador": user, "Senha": password},
            timeout=self.opts.get("timeout", 90),
        )
        resp.raise_for_status()
        payload = resp.json()
        token = (payload.get("items") or payload).get("tokenautenticacao") \
            or (payload.get("items") or payload).get("tokenautenticação")
        if not token:
            raise http.FetchError(f"la respuesta de OAuth no trae token: {payload}")
        return token

    # ------------------------------------------------------------- discovery
    def discover(self, name_fragment: str) -> list[dict[str, str]]:
        """Busca estaciones por nombre en el inventario de ANA.

        Es la vía para resolver un `ana_code` desconocido sin adivinarlo.
        """
        resp = http.get(
            f"{self.legacy_base}/HidroInventario",
            params={
                "codEstDE": "", "codEstATE": "", "tpEst": "", "nmEst": name_fragment,
                "nmRio": "", "codSubBacia": "", "codBacia": "", "nmMunicipio": "",
                "nmEstado": "", "sgResp": "", "sgOper": "", "telemetrica": "",
            },
            timeout=self.opts.get("timeout", 90),
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        found: list[dict[str, str]] = []
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1].lower() != "table":
                continue
            row = {
                c.tag.rsplit("}", 1)[-1]: (c.text or "").strip()
                for c in node
            }
            if row.get("Codigo"):
                found.append({
                    "codigo": row.get("Codigo", ""),
                    "nome": row.get("Nome", ""),
                    "rio": row.get("nmRio", "") or row.get("NomeRio", ""),
                    "responsavel": row.get("ResponsavelSigla", ""),
                    "operadora": row.get("OperadoraSigla", ""),
                    "telemetrica": row.get("TipoEstacaoTelemetrica", ""),
                    "latitude": row.get("Latitude", ""),
                    "longitude": row.get("Longitude", ""),
                })
        return found

    # ------------------------------------------------------------------ API
    def probe(self) -> ProbeResult:
        stations = [s for s in self.cfg.stations if s.ana_code]
        unresolved = [s.name for s in self.cfg.stations if not s.ana_code]
        result = ProbeResult(source=self.name, ok=False,
                             url=f"{self.legacy_base}/DadosHidrometeorologicos")
        if unresolved:
            result.notes.append(
                f"sin código ANA: {', '.join(unresolved)} "
                f"-> resolver con: iguacu-monitor discover \"<nombre>\""
            )
        if not stations:
            result.error = "ninguna estación tiene ana_code configurado"
            return result

        end = date.today()
        start = end - timedelta(days=2)
        total, samples = 0, []
        for site in stations:
            try:
                nodes = self._legacy_series(int(site.ana_code), start, end)
                obs = self._parse_records(nodes, site.key)
            except Exception as exc:  # noqa: BLE001 - el probe reporta, no corta
                result.notes.append(f"{site.name} ({site.ana_code}): {exc}")
                continue
            total += len(obs)
            result.notes.append(f"{site.name} ({site.ana_code}): {len(obs)} obs en 48 h")
            if obs and not samples:
                last = sorted(obs, key=lambda o: o.timestamp)[-3:]
                samples = [f"{o.timestamp:%Y-%m-%d %H:%M}Z {o.variable}={o.value}" for o in last]

        result.parsed = total
        result.ok = total > 0
        result.sample = "\n".join(samples)
        if total == 0:
            result.error = "ninguna estación devolvió datos"
        return result

    def fetch(self, start: date, end: date) -> list[Observation]:
        out: list[Observation] = []
        for site in self.cfg.stations:
            if not site.ana_code:
                continue
            nodes = self._legacy_series(int(site.ana_code), start, end)
            out.extend(self._parse_records(nodes, site.key))
        return out
