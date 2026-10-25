"""COPEL — Monitoramento Hidrológico de Bacias (MHB), cuenca del Iguazú.

Da, con una hora de rezago, las usinas de la cascada (nivel, volumen útil,
afluente, turbinado, vertido y defluente) y los puestos hidrométricos
(régua, caudal y lluvia horaria), cada uno con 72 h de historia. Las usinas
llegan unas 2 h antes que en ONS. También publica la previsión de SIMEPAR
para União da Vitória, con y sin la lluvia prevista.

La página es PrimeFaces y no tiene API; hay que recorrerla como un
navegador, con la misma sesión:

1. GET  monitoramento.jsf                  -> ViewState
2. POST form:baciaIguacuLink (ajax)         -> redirige a bacia-iguacu.jsf
3. GET  bacia-iguacu.jsf                    -> enlaces y diálogos de cada punto
4. por punto: POST su enlace (lo deja seleccionado en la sesión) y POST
   `<diálogo>_contentLoad` -> tabla horaria de las últimas 72 h
5. POST el enlace de previsión               -> redirige a previsao.jsf

Los números vienen en formato brasileño ("1.410" son 1410 m³/s, "4,436"
son 4,436 m) y "s/l" es sin lectura. Las horas son de Brasilia (UTC−3).
"""

from __future__ import annotations

import html as htmllib
import json
import re
from datetime import date, datetime, timedelta, timezone

import requests

from iguacu_monitor import http
from iguacu_monitor.model import (FC_BASE, FC_LEVEL, FC_LEVEL_DRY, FC_QOBS, FC_QOBS_DRY, LEVEL,
                                  PRECIP, QAFL, QDEF, QOBS, QTUR, QVER, STORAGE, Observation,
                                  ProbeResult, normalize_label)
from iguacu_monitor.sources.base import Source

BASE = "https://www.copel.com/mhbweb/paginas"
BRASILIA = timezone(timedelta(hours=-3))
AJAX = {"Faces-Request": "partial/ajax", "X-Requested-With": "XMLHttpRequest"}

# Encabezado normalizado -> variable. "Nível de água" es el nivel del
# embalse en las usinas; en los puestos es la cota absoluta, y el nivel que
# se compara con ANA es la lectura de la régua.
PLANT_COLUMNS = {
    "nivel de agua m": LEVEL,
    "volume util": STORAGE,
    "vazao afluente m3 s": QAFL,
    "vazao turbinada m3 s": QTUR,
    "vazao vertida m3 s": QVER,
    "vazao defluente m3 s": QDEF,
}
STATION_COLUMNS = {
    "leitura da regua m": LEVEL,
    "vazao m3 s": QOBS,
    "precipitacao mm": PRECIP,
}


def parse_number(text: str) -> float | None:
    text = text.strip()
    if not text or text.lower() in ("s/l", "-", "—"):
        return None
    try:
        return float(text.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def parse_stamp(text: str) -> datetime | None:
    """'25/10/26 09h' o '25/10/2026 21:00', hora de Brasilia, a UTC."""
    text = text.strip()
    for fmt in ("%d/%m/%y %Hh", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=BRASILIA).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _text(fragment: str) -> str:
    return htmllib.unescape(re.sub(r"<[^>]+>|\s+", " ", fragment)).strip()


def view_state(page: str) -> str:
    m = re.search(r'name="javax\.faces\.ViewState"[^>]*value="([^"]+)"', page)
    if not m:
        raise http.FetchError("la página de COPEL no trae ViewState")
    return m.group(1)


def points(page: str) -> dict[str, tuple[str, str]]:
    """Título normalizado del punto -> (id del enlace, id del diálogo).

    El título es el nombre antes de " - " ("Foz do Areia - Usina ..." ->
    "foz do areia"). El enlace abre el diálogo por su widgetVar.
    """
    dialogs = re.findall(r'<div id="([^"]+:formDialog:janela)"[^>]*>.*?class="ui-dialog-title">([^<]+)</span>',
                         page, flags=re.S)
    widget = {dlg: var for var, dlg in re.findall(r'PrimeFaces\.cw\("Dialog","(\w+)",\{id:"([^"]+)"', page)}
    link = {var: lid for lid, var in re.findall(
        r'<a id="([^"]+)"[^>]*onclick="[^"]*PF\((?:&#39;|\')(\w+)(?:&#39;|\')\)\.show\(\)', page)}
    out = {}
    for dlg, title in dialogs:
        name = normalize_label(htmllib.unescape(title).split(" - ")[0])
        lid = link.get(widget.get(dlg, ""))
        if lid:
            out[name] = (lid, dlg)
    return out


def parse_table(fragment: str) -> tuple[list[str], list[list[str]]]:
    """Encabezados y filas (con fecha) de la tabla horaria de un diálogo."""
    heads = [_text(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", fragment, flags=re.S)]
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", fragment, flags=re.S):
        cells = [_text(td) for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S)]
        if cells and parse_stamp(cells[0]):
            rows.append(cells)
    return heads, rows


def table_observations(site, heads: list[str], rows: list[list[str]], source: str = "copel_mhb") -> list[Observation]:
    columns = PLANT_COLUMNS if site.kind == "plant" else STATION_COLUMNS
    index = {i: columns[normalize_label(h)] for i, h in enumerate(heads) if normalize_label(h) in columns}
    out = []
    for cells in rows:
        stamp = parse_stamp(cells[0])
        for i, var in index.items():
            value = parse_number(cells[i]) if i < len(cells) else None
            if stamp is not None and value is not None:
                out.append(Observation(source=source, site=site.key, variable=var, timestamp=stamp, value=value))
    return out


def parse_forecast(page: str) -> tuple[datetime | None, float | None, list[tuple]]:
    """(último dato observado que usó el modelo, su nivel, pronóstico).

    Cada punto del pronóstico es (instante, régua, caudal, régua sin
    lluvia, caudal sin lluvia). El instante del último dato sale de la serie
    observada del gráfico: la etiqueta "Último valor considerado" no coincide
    ni con la hora local ni con UTC.
    """
    forecast = []
    body = page[page.find("tabela-previsao-cabecalho"):]
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body, flags=re.S):
        cells = [_text(td) for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S)]
        if len(cells) >= 7 and parse_stamp(cells[0]):
            forecast.append((parse_stamp(cells[0]), parse_number(cells[1]), parse_number(cells[3]),
                             parse_number(cells[4]), parse_number(cells[6])))
    base_t, base_level = None, None
    m = re.search(r"data:(\[\[\[.*?\]\]\])", page, flags=re.S)
    if m:
        series = json.loads(m.group(1))
        observed = [(parse_stamp(t), v) for t, v in series[-1] if v is not None]
        observed = [(t, v) for t, v in observed if t is not None]
        if observed:
            base_t, base_level = max(observed)
    return base_t, base_level, forecast


class CopelMHB(Source):
    name = "copel_mhb"

    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self._session: requests.Session | None = None
        self._page: str | None = None

    @property
    def url(self) -> str:
        return f"{BASE}/bacia-iguacu.jsf"

    def _timeout(self) -> float:
        return float(self.opts.get("timeout", 60))

    def _ajax(self, page_url: str, source: str, state: str, form: str = "form",
              extra: dict | None = None) -> str:
        data = {"javax.faces.partial.ajax": "true", "javax.faces.source": source,
                "javax.faces.partial.execute": source, source: source,
                form: form, "javax.faces.ViewState": state}
        data.update(extra or {})
        resp = http.post(page_url, data=data, headers=AJAX, session=self._session, timeout=self._timeout())
        resp.raise_for_status()
        return resp.text

    def _open(self) -> str:
        """Sesión nueva en la página de la cuenca del Iguazú."""
        self._session = requests.Session()
        first = http.get(f"{BASE}/monitoramento.jsf", session=self._session, timeout=self._timeout())
        first.raise_for_status()
        self._ajax(f"{BASE}/monitoramento.jsf", "form:baciaIguacuLink", view_state(first.text))
        page = http.get(self.url, session=self._session, timeout=self._timeout())
        page.raise_for_status()
        self._page = page.text
        return page.text

    def _point(self, page: str, link_id: str, dialog_id: str) -> str:
        state = view_state(page)
        self._ajax(self.url, link_id, state)
        form = dialog_id.rsplit(":janela", 1)[0]
        return self._ajax(self.url, dialog_id, state, form=form, extra={
            "javax.faces.partial.render": dialog_id, f"{dialog_id}_contentLoad": "true",
        })

    def _forecast(self, page: str) -> list[Observation]:
        site_key = self.opts.get("forecast_site")
        m = re.search(r'<a id="([^"]+)"[^>]*>\s*Previs[^<]*Uni[^<]*Vit', page)
        if not site_key or not m:
            return []
        self._ajax(self.url, m.group(1), view_state(page))
        resp = http.get(f"{BASE}/previsao.jsf", session=self._session, timeout=self._timeout())
        resp.raise_for_status()
        base_t, base_level, rows = parse_forecast(resp.text)
        if base_t is None:
            return []
        out = [Observation(source=self.name, site=site_key, variable=FC_BASE, timestamp=base_t, value=base_level)]
        for t, level, q, level_dry, q_dry in rows:
            for var, value in ((FC_LEVEL, level), (FC_QOBS, q), (FC_LEVEL_DRY, level_dry), (FC_QOBS_DRY, q_dry)):
                if value is not None:
                    out.append(Observation(source=self.name, site=site_key, variable=var, timestamp=t, value=value))
        return out

    def fetch(self, start: date, end: date) -> list[Observation]:
        """Las últimas 72 h de cada punto configurado y la previsión.

        La página no acepta otro rango sin cambiar las fechas del diálogo;
        con corridas cada 2 h, 72 h sobran.
        """
        page = self._open()
        found = points(page)
        out: list[Observation] = []
        for site in self.cfg.sites:
            if not site.copel_label:
                continue
            ids = found.get(normalize_label(site.copel_label))
            if ids is None:
                continue
            out.extend(table_observations(site, *parse_table(self._point(page, *ids))))
        out.extend(self._forecast(page))
        return out

    def probe(self) -> ProbeResult:
        result = ProbeResult(source=self.name, ok=False, url=self.url)
        try:
            obs = self.fetch(date.today(), date.today())
        except (http.FetchError, requests.RequestException) as exc:
            result.error = str(exc)
            return result
        found = points(self._page or "")
        wanted = {normalize_label(s.copel_label): s.key for s in self.cfg.sites if s.copel_label}
        missing = sorted(k for k in wanted if k not in found)
        result.parsed = len(obs)
        result.ok = bool(obs)
        by_site: dict[str, int] = {}
        for o in obs:
            by_site[o.site] = by_site.get(o.site, 0) + 1
        result.notes = [f"{len(found)} puntos en la página", "por sitio: " + ", ".join(f"{k} {v}" for k, v in by_site.items())]
        if missing:
            result.notes.append("sin punto en la página para: " + ", ".join(missing))
        if not obs:
            result.error = "ningún punto configurado devolvió datos"
        return result
