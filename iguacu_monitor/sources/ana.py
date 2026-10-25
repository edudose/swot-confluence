"""ANA — caudal observado en estaciones telemétricas.

Dos caminos:

* `legacy`  -> telemetriaws1.ana.gov.br/ServiceANA.asmx, XML, sin credenciales.
* `hidrowebservice` -> API nueva (www.ana.gov.br/hidrowebservice), con
  usuario y contraseña que ANA entrega por correo. El token se pide en
  /EstacoesTelemetricas/OAUth/v1, dura una hora y se renueva solo. Trae
  varias estaciones por pedido y un indicador de calidad por dato.

Con `mode: auto` (por defecto) se usa la API nueva si están ANA_HWS_USER y
ANA_HWS_PASS en el entorno, y el servicio legacy si no.

Ambos entregan la cota en centímetros y la hora en Brasilia sin declararlo;
la API nueva filtra además por la fecha LOCAL, no por la UTC.

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

# La API nueva rechaza (HTTP 406) pedidos de más de 10 estaciones.
HWS_MAX_STATIONS = 10

# Ventanas que acepta la API nueva, de menor a mayor.
HWS_RANGES = ((2, "DIAS_2"), (7, "DIAS_7"), (14, "DIAS_14"), (21, "DIAS_21"), (30, "DIAS_30"))

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def parse_validade(text: str | None) -> datetime | None:
    """'Sat Oct 24 22:30:09 GMT-03:00 2026' -> datetime con zona."""
    m = re.search(r"\w{3} (\w{3}) (\d{1,2}) (\d{2}):(\d{2}):(\d{2}) GMT([+-])(\d{2}):(\d{2}) (\d{4})",
                  text or "")
    if not m or m.group(1) not in _MONTHS:
        return None
    mon, day, hh, mm, ss, sign, oh, om, year = m.groups()
    offset = timedelta(hours=int(oh), minutes=int(om)) * (1 if sign == "+" else -1)
    return datetime(int(year), _MONTHS[mon], int(day), int(hh), int(mm), int(ss),
                    tzinfo=timezone(offset))


def hws_windows(start: date, end: date) -> list[tuple[date, str]]:
    """Cubre [start, end] con pedidos (fecha de búsqueda, rango) de hasta 30 días.

    La API devuelve los días del rango que terminan en la fecha de búsqueda.
    """
    out: list[tuple[date, str]] = []
    cursor = end
    while cursor >= start:
        span = (cursor - start).days + 1
        days, label = next(((d, l) for d, l in HWS_RANGES if d >= span), HWS_RANGES[-1])
        out.append((cursor, label))
        cursor -= timedelta(days=days)
    return out

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

    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self._token: str | None = None
        self._token_until: datetime | None = None

    @property
    def uses_hws(self) -> bool:
        mode = self.opts.get("mode", "auto")
        if mode == "hidrowebservice":
            return True
        if mode == "legacy":
            return False
        return bool(os.getenv("ANA_HWS_USER") and os.getenv("ANA_HWS_PASS"))

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
    @property
    def hws_base(self) -> str:
        return self.opts.get("hws_base", "https://www.ana.gov.br/hidrowebservice")

    def _hws_token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._token and self._token_until and now < self._token_until - timedelta(minutes=2):
            return self._token
        user, password = os.getenv("ANA_HWS_USER"), os.getenv("ANA_HWS_PASS")
        if not user or not password:
            raise http.FetchError(
                "faltan credenciales: definí ANA_HWS_USER y ANA_HWS_PASS en el entorno. "
                "Se piden por correo a hidro@ana.gov.br o telemetria@ana.gov.br."
            )
        resp = http.get(
            f"{self.hws_base}/EstacoesTelemetricas/OAUth/v1",
            headers={"Identificador": user, "Senha": password},
            timeout=self.opts.get("timeout", 90),
        )
        resp.raise_for_status()
        items = resp.json().get("items") or {}
        token = items.get("tokenautenticacao") or items.get("tokenautenticação")
        if not token:
            # El mensaje de ANA dice qué falló (credencial vencida, etc.);
            # nunca se incluye el token ni la contraseña en el error.
            raise http.FetchError(f"ANA rechazó la autenticación: {items.get('respostaautenticacao') or items.get('retorno')}")
        self._token = token
        self._token_until = parse_validade(items.get("validade")) or now + timedelta(minutes=55)
        return token

    def _hws_rows(self, codes: list[int], start: date, end: date) -> list[dict]:
        # La API filtra por fecha local: nunca pedir un día que en Brasilia
        # todavía no empezó (de noche en UTC ya es "mañana").
        end = min(end, datetime.now(BRASILIA).date())
        rows: list[dict] = []
        for i in range(0, len(codes), HWS_MAX_STATIONS):
            batch = ",".join(str(c) for c in codes[i:i + HWS_MAX_STATIONS])
            for busca, label in hws_windows(start, end):
                resp = http.get(
                    f"{self.hws_base}/EstacoesTelemetricas/HidroinfoanaSerieTelemetricaAdotada/v2",
                    params={
                        "Codigos_Estacoes": batch,
                        "Tipo Filtro Data": "DATA_LEITURA",
                        "Data de Busca (yyyy-MM-dd)": busca.isoformat(),
                        "Range Intervalo de busca": label,
                    },
                    headers={"Authorization": f"Bearer {self._hws_token()}"},
                    timeout=self.opts.get("timeout", 90),
                )
                resp.raise_for_status()
                rows.extend(resp.json().get("items") or [])
        return rows

    def _parse_hws(self, rows: list[dict], start: date, end: date) -> list[Observation]:
        stations = {str(s.ana_code): s.key for s in self.cfg.stations if s.ana_code}
        gauges = {str(g.code): g.key for g in self.cfg.gauges if g.network == "ana"}
        all_fields = (("Vazao_Adotada", QOBS, 1.0),
                      ("Cota_Adotada", LEVEL, 0.01),   # centímetros
                      ("Chuva_Adotada", PRECIP, 1.0))
        rain_only = (("Chuva_Adotada", PRECIP, 1.0),)
        out: list[Observation] = []
        seen: set[tuple] = set()
        for row in rows:
            code = str(row.get("codigoestacao", "")).strip()
            stamp = str(row.get("Data_Hora_Medicao") or "")[:19]
            # Una estación puede ser a la vez del tronco y pluviómetro (União
            # da Vitória): la lluvia va a las dos claves.
            targets = []
            if code in stations:
                targets.append((stations[code], all_fields))
            if code in gauges:
                targets.append((gauges[code], rain_only))
            if not targets or not stamp:
                continue
            try:
                local = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BRASILIA)
            except ValueError:
                continue
            if not (start <= local.date() <= end):
                continue
            when = local.astimezone(timezone.utc)
            for key, fields in targets:
                for field, var, scale in fields:
                    value = _to_float(row.get(field))
                    if value is None or (key, var, when) in seen:
                        continue
                    seen.add((key, var, when))
                    out.append(Observation(
                        source=self.name, site=key, variable=var, timestamp=when,
                        value=value * scale,
                        meta={"status": row.get(f"{field}_Status")},
                    ))
        return out

    def discover_gauges(self) -> tuple[list[dict], list[str]]:
        """Pluviómetros telemétricos vivos dentro de la cuenca.

        Devuelve (pluviómetros, notas). Filtra el inventario por subcuenca,
        telemetría y operación, recorta con la cuenca, y conserva las
        estaciones que reportaron lluvia en las últimas 48 h: el inventario
        no dice qué sensores transmiten de verdad.
        """
        opts = self.cfg.gauges_opts
        token = self._hws_token()
        resp = http.get(
            f"{self.hws_base}/EstacoesTelemetricas/HidroInventarioEstacoes/v1",
            params={"Código da Bacia": str(opts.get("ana_bacia", 6))},
            headers={"Authorization": f"Bearer {token}"},
            timeout=max(self.opts.get("timeout", 90), 240),
        )
        resp.raise_for_status()
        inventory = resp.json().get("items") or []

        sub = str(opts.get("ana_sub_bacia", ""))
        candidates: list[dict] = []
        for row in inventory:
            lat, lon = _to_float(row.get("Latitude")), _to_float(row.get("Longitude"))
            if (sub and str(row.get("Sub_Bacia_Codigo")) != sub) or lat is None or lon is None:
                continue
            if str(row.get("Tipo_Estacao_Telemetrica")) != "1" or str(row.get("Operando")) != "1":
                continue
            if not self.cfg.basin.contains(lon, lat):
                continue
            candidates.append(row)

        end = datetime.now(BRASILIA).date()
        rows = self._hws_rows([int(r["codigoestacao"]) for r in candidates], end - timedelta(days=1), end)
        counts: dict[str, int] = {}
        for row in rows:
            if _to_float(row.get("Chuva_Adotada")) is not None:
                code = str(row.get("codigoestacao", "")).strip()
                counts[code] = counts.get(code, 0) + 1

        minimum = int(opts.get("min_records_48h", 24))
        gauges = [
            {
                "code": int(r["codigoestacao"]),
                "name": str(r.get("Estacao_Nome", "")).strip(),
                "operator": str(r.get("Operadora_Sigla", "")).strip(),
                "lat": round(_to_float(r["Latitude"]), 4),
                "lon": round(_to_float(r["Longitude"]), 4),
                "records_48h": counts.get(str(r["codigoestacao"]), 0),
            }
            for r in candidates if counts.get(str(r["codigoestacao"]), 0) >= minimum
        ]
        gauges.sort(key=lambda g: g["code"])
        notes = [
            f"{len(inventory)} estaciones en el inventario de la bacia {opts.get('ana_bacia', 6)}",
            f"{len(candidates)} telemétricas operando en la subcuenca {sub} dentro de la cuenca",
            f"{len(gauges)} reportaron lluvia al menos {minimum} veces en 48 h",
        ]
        return gauges, notes

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
        url = (f"{self.hws_base}/EstacoesTelemetricas/HidroinfoanaSerieTelemetricaAdotada/v2"
               if self.uses_hws else f"{self.legacy_base}/DadosHidrometeorologicos")
        result = ProbeResult(source=self.name, ok=False, url=url)
        result.notes.append("vía API nueva (HidroWebService, con credenciales)" if self.uses_hws
                            else "vía servicio legacy (sin credenciales)")
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
        by_site: dict[str, list[Observation]] = {}
        if self.uses_hws:
            try:
                rows = self._hws_rows([int(s.ana_code) for s in stations], start, end)
                for o in self._parse_hws(rows, start, end):
                    by_site.setdefault(o.site, []).append(o)
            except Exception as exc:  # noqa: BLE001
                result.error = str(exc)
                return result
        for site in stations:
            try:
                if self.uses_hws:
                    obs = by_site.get(site.key, [])
                else:
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
        if self.uses_hws:
            codes = [int(s.ana_code) for s in self.cfg.stations if s.ana_code]
            codes += [g.code for g in self.cfg.gauges if g.network == "ana" and g.code not in codes]
            return self._parse_hws(self._hws_rows(codes, start, end), start, end)
        out: list[Observation] = []
        for site in self.cfg.stations:
            if not site.ana_code:
                continue
            nodes = self._legacy_series(int(site.ana_code), start, end)
            out.extend(self._parse_records(nodes, site.key))
        return out
