"""CEMADEN: lectura del JSONP y combinación con ANA en la ventana de 24 h."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from iguacu_monitor import config, panel, report
from iguacu_monitor.model import PRECIP, PRECIP_24H, Observation
from iguacu_monitor.sources import cemaden

NOW = datetime(2026, 10, 25, 13, 0, tzinfo=timezone.utc)

JSONP = ('estacoes([{"atualizado":"2026-10-25 12:48:43 UTC","estacao":['
         '{"acumulado":104.8,"cidade":"CRUZ MACHADO","codestacao":"410680301A","latitude":-26.0,'
         '"longitude":-51.3,"nomeestacao":"G2","sigla":"CEMADEN","status":0,"tipoestacao":"Pluviométrica"},'
         '{"acumulado":null,"cidade":"FORTALEZA","codestacao":"230440016A","latitude":-3.77,'
         '"longitude":-38.56,"nomeestacao":"AC Parangaba","sigla":"CEMADEN","status":0,"tipoestacao":"Pluviométrica"}'
         ']}])')


def _cfg():
    return config.load("config/iguacu.yml")


def _frame(obs):
    return pd.DataFrame([o.as_row() for o in obs])


def _cem(n, mm, at):
    gauges = [g for g in _cfg().gauges if g.network == "cemaden"][:n]
    return [Observation(source="cemaden", site=g.key, variable=PRECIP_24H, timestamp=at, value=mm)
            for g in gauges]


def _ana(n, per_hour, end, hours=30):
    gauges = [g for g in _cfg().gauges if g.network == "ana"][:n]
    return [Observation(source="ana", site=g.key, variable=PRECIP, timestamp=end - timedelta(hours=h), value=per_hour)
            for g in gauges for h in range(hours)]


def test_parse_reads_update_time_and_stations():
    stamp, stations = cemaden.parse(JSONP)
    assert stamp == datetime(2026, 10, 25, 12, 48, 43, tzinfo=timezone.utc)
    assert [s["codestacao"] for s in stations] == ["410680301A", "230440016A"]


def test_catalog_loads_as_its_own_network():
    cem = [g for g in _cfg().gauges if g.network == "cemaden"]
    assert cem and all(g.key.startswith("cem_") and isinstance(g.code, str) for g in cem)


def test_cemaden_joins_ana_in_the_24h_window_only():
    obs = _ana(10, 1.0, NOW - timedelta(hours=1)) + _cem(5, 24.0, NOW)
    g = report.gauge_rain(_frame(obs), _cfg())
    assert g[24].by_network == {"ANA": 10, "CEMADEN": 5}
    assert g[24].until == NOW
    assert g[6].by_network == {"ANA": 10}


def test_stale_ana_leaves_the_24h_window_to_cemaden():
    obs = _ana(10, 1.0, NOW - timedelta(hours=9)) + _cem(5, 24.0, NOW)
    g = report.gauge_rain(_frame(obs), _cfg())
    assert g[24].by_network == {"CEMADEN": 5}
    assert g[6].until == NOW - timedelta(hours=9)


def test_stale_cemaden_is_left_out():
    obs = _ana(10, 1.0, NOW) + _cem(5, 24.0, NOW - timedelta(hours=9))
    g = report.gauge_rain(_frame(obs), _cfg())
    assert g[24].by_network == {"ANA": 10}


def test_panel_marks_cemaden_and_hides_stale_short_windows():
    cfg = _cfg()
    obs = _ana(10, 1.0, NOW - timedelta(hours=9)) + _cem(8, 24.0, NOW)
    page = panel.render(report.build(_frame(obs), cfg, now=NOW), cfg, fragment=True)
    assert "pluviómetro CEMADEN" in page
    summary = page[page.index('<ul class="summary">'):page.index("</ul>", page.index('<ul class="summary">'))]
    assert "Lluvia medida 24 h</span>" in summary          # la de 24 h está al día
    assert "1 h " not in summary and "6 h " not in summary  # las de ANA tienen 9 h


@pytest.mark.parametrize("mm,excluded", [(600.0, True), (300.0, False)])
def test_cemaden_total_above_24h_envelope_is_excluded(mm, excluded):
    obs = _ana(10, 1.0, NOW) + _cem(1, mm, NOW)
    g = report.gauge_rain(_frame(obs), _cfg())
    reasons = [why for _, _, why in g[24].excluded]
    assert (report.IMPLAUSIBLE in reasons) is excluded
