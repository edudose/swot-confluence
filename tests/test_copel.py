"""COPEL MHB: navegación PrimeFaces, tablas horarias y previsión de SIMEPAR.

Los fragmentos imitan la estructura real de la página (25-oct-2026), sin
copiarla: el pie de la página trae nombres del personal de COPEL.
"""

from datetime import datetime, timezone

import pytest

from iguacu_monitor import config
from iguacu_monitor.model import LEVEL, PRECIP, QAFL, QDEF, QOBS, QVER, STORAGE
from iguacu_monitor.sources import copel_mhb as mhb

UTC = timezone.utc

PAGE = """
<a id="form:j_idt100:10:j_idt102" href="#" class="ui-commandlink ui-widget"
   onclick="PrimeFaces.ab({s:&quot;form:j_idt100:10:j_idt102&quot;,f:&quot;form&quot;,ps:true,onco:function(xhr,status,args){PF('26Modal').show();;}});return false;">União da Vitória</a>
<a id="form:j_idt90:3:j_idt92" href="#" class="ui-commandlink ui-widget"
   onclick="PrimeFaces.ab({s:&quot;form:j_idt90:3:j_idt92&quot;,f:&quot;form&quot;,ps:true,onco:function(xhr,status,args){PF('7Modal').show();;}});return false;">Foz do Areia</a>
<a id="form:j_idt106" href="#" class="ui-commandlink ui-widget" onclick="PrimeFaces.ab({s:&quot;form:j_idt106&quot;});return false;">Previsão de vazões e níveis para União da Vitória </a>
<form id="j_idt164:6:formDialog"><div id="j_idt164:6:formDialog:janela" class="ui-dialog"><div class="ui-dialog-titlebar"><span id="t6" class="ui-dialog-title">Foz do Areia - Usina Hidrelétrica Governador Bento Munhoz da Rocha Netto</span></div></div>
<script>$(function(){PrimeFaces.cw("Dialog","7Modal",{id:"j_idt164:6:formDialog:janela",width:"100%",dynamic:true});});</script></form>
<form id="j_idt164:33:formDialog"><div id="j_idt164:33:formDialog:janela" class="ui-dialog"><div class="ui-dialog-titlebar"><span id="t33" class="ui-dialog-title">União da Vitória</span></div></div>
<script>$(function(){PrimeFaces.cw("Dialog","26Modal",{id:"j_idt164:33:formDialog:janela",width:"100%",dynamic:true});});</script></form>
<input type="hidden" name="javax.faces.ViewState" id="j_id1:javax.faces.ViewState:0" value="-7987:5716" />
"""


def _th(text):
    return f'<th role="columnheader"><span class="ui-column-title"><span title="{text}">{text}</span></span></th>'


def _tr(cells):
    tds = "".join(f'<td role="gridcell"><span class="coluna-tabela">{c}</span></td>' for c in cells[1:])
    return f'<tr role="row"><td role="gridcell">{cells[0]}</td>{tds}</tr>'


PLANT_DIALOG = ("<table><thead><tr>" + "".join(_th(h) for h in (
    "Data/hora", "Nível de água (m)", "Volume útil (%)", "Nível jusante (m)", "Vazão afluente (m³/s)",
    "Vazão turbinada (m³/s)", "Vazão vertida (m³/s)", "Vazão defluente (m³/s)", "Precipitação (mm)"))
    + "</tr></thead><tbody>"
    + _tr(["25/10/26 10h", "s/l", "s/l", "609,840", "s/l", "s/l", "s/l", "s/l", "s/l", "s/l"])
    + _tr(["25/10/26 09h", "738,097", "86,60", "609,750", "4.710", "1.090", "3.300", "4.390", "0,0", "0,0"])
    + "</tbody></table>")

STATION_DIALOG = ("<table><thead><tr>" + "".join(_th(h) for h in (
    "Data/hora", "Leitura da régua (m)", "Nível de água (m)", "Vazão (m³/s)", "Precipitação (mm)",
    "Precipitação acumulada (mm)"))
    + "</tr></thead><tbody>"
    + _tr(["25/10/26 09h", "6,150", "745,760", "2.060", "0,0", "0,2"])
    + _tr(["25/10/26 00h", "6,040", "745,650", "1.996", "0,0", "138,4"])
    + "</tbody></table>")

FORECAST = """
<tr role="row" class="tabela-previsao-cabecalho"><th>Leitura da régua (m)</th></tr></thead>
<tbody><tr data-ri="0" role="row"><td role="gridcell"><span>25/10/2026 21:00</span></td><td>6,30</td><td>745,91</td><td>2.100</td><td>6,30</td><td>745,91</td><td>2.100</td></tr>
<tr data-ri="1" role="row"><td role="gridcell"><span>27/10/2026 03:00</span></td><td>6,51</td><td>746,12</td><td>2.210</td><td>6,49</td><td>746,10</td><td>2.200</td></tr></tbody>
<label>Último valor considerado:</label><label>25/10/2026 14:00</label>
<script>$(function(){PrimeFaces.cw("Chart","graficoLinhas",{id:"g",type:"line",data:[[["25\\/10\\/26 08h",null],["25\\/10\\/26 21h",6.3]],[["25\\/10\\/26 21h",6.3]],[["25\\/10\\/26 08h",6.14],["25\\/10\\/26 09h",6.15]]],shadow:false});});</script>
"""


def _cfg():
    return config.load("config/iguacu.yml")


@pytest.mark.parametrize("text,value", [("1.410", 1410.0), ("17.300", 17300.0), ("4,436", 4.436),
                                        ("62,5", 62.5), ("609,840", 609.84), ("s/l", None), ("", None)])
def test_numbers_are_brazilian(text, value):
    assert mhb.parse_number(text) == value


def test_stamps_are_brasilia_time():
    assert mhb.parse_stamp("25/10/26 09h") == datetime(2026, 10, 25, 12, tzinfo=UTC)
    assert mhb.parse_stamp("25/10/2026 21:00") == datetime(2026, 10, 26, 0, tzinfo=UTC)
    assert mhb.parse_stamp("Data/hora") is None


def test_points_pair_each_dialog_with_its_link():
    found = mhb.points(PAGE)
    assert found["foz do areia"] == ("form:j_idt90:3:j_idt92", "j_idt164:6:formDialog:janela")
    assert found["uniao da vitoria"] == ("form:j_idt100:10:j_idt102", "j_idt164:33:formDialog:janela")
    assert mhb.view_state(PAGE) == "-7987:5716"


def test_plant_table_skips_missing_readings():
    site = _cfg().site("foz_do_areia")
    obs = mhb.table_observations(site, *mhb.parse_table(PLANT_DIALOG))
    got = {o.variable: o.value for o in obs}
    assert got == {LEVEL: 738.097, STORAGE: 86.6, QAFL: 4710.0, "qtur": 1090.0, QVER: 3300.0, QDEF: 4390.0}
    assert {o.timestamp for o in obs} == {datetime(2026, 10, 25, 12, tzinfo=UTC)}   # la fila 's/l' no entra


def test_station_level_is_the_staff_gauge_reading():
    site = _cfg().site("uniao_da_vitoria")
    obs = mhb.table_observations(site, *mhb.parse_table(STATION_DIALOG))
    at_9 = {o.variable: o.value for o in obs if o.timestamp == datetime(2026, 10, 25, 12, tzinfo=UTC)}
    assert at_9 == {LEVEL: 6.15, QOBS: 2060.0, PRECIP: 0.0}     # régua, no la cota absoluta


def test_forecast_uses_the_chart_for_the_last_observation():
    base_t, base_level, rows = mhb.parse_forecast(FORECAST)
    assert (base_t, base_level) == (datetime(2026, 10, 25, 12, tzinfo=UTC), 6.15)
    assert rows[-1] == (datetime(2026, 10, 27, 6, tzinfo=UTC), 6.51, 2210.0, 6.49, 2200.0)


# ------------------------------------------------- una fuente por sitio ----
import pandas as pd

from iguacu_monitor import report
from iguacu_monitor.model import Observation

NOW = datetime(2026, 10, 25, 13, tzinfo=UTC)


def _series(source, site, var, last_hour, value, hours=6):
    return [Observation(source=source, site=site, variable=var,
                        timestamp=datetime(2026, 10, 25, last_hour - h, tzinfo=UTC), value=value)
            for h in range(hours)]


def _build(obs):
    return report.build(pd.DataFrame([o.as_row() for o in obs]), _cfg(), now=NOW)


def _state(rep, key):
    return next(s for s in rep.sites if s.key == key)


def test_plants_take_copel_when_it_is_fresher_than_ons():
    obs = _series("ons", "foz_do_areia", QDEF, 10, 4613.0) + _series("copel_mhb", "foz_do_areia", QDEF, 12, 4390.0)
    st = _state(_build(obs), "foz_do_areia")
    assert st.values[QDEF] == 4390.0 and st.sources[QDEF] == "copel_mhb"
    assert {v for _, v in st.series} == {4390.0}               # sin mezclar fuentes


def test_plants_fall_back_to_ons_when_copel_lags():
    obs = _series("ons", "foz_do_areia", QDEF, 12, 4613.0) + _series("copel_mhb", "foz_do_areia", QDEF, 9, 4390.0)
    assert _state(_build(obs), "foz_do_areia").sources[QDEF] == "ons"


@pytest.mark.parametrize("ana_hour,expected", [(12, "ana"), (10, "ana"), (8, "copel_mhb")])
def test_trunk_stations_keep_ana_unless_it_is_stale(ana_hour, expected):
    obs = _series("ana", "porto_capanema", QOBS, ana_hour, 17049.0) + _series("copel_mhb", "porto_capanema", QOBS, 12, 17300.0)
    assert _state(_build(obs), "porto_capanema").sources[QOBS] == expected


def _uv_with_forecast(levels=(6.30, 6.36, 6.51)):
    from iguacu_monitor.model import FC_BASE, FC_LEVEL, FC_LEVEL_DRY, FC_QOBS, FC_QOBS_DRY
    obs = _series("ana", "uniao_da_vitoria", QOBS, 12, 2057.0) + _series("ana", "uniao_da_vitoria", LEVEL, 12, 6.15)
    base = datetime(2026, 10, 25, 12, tzinfo=UTC)
    obs.append(Observation(source="copel_mhb", site="uniao_da_vitoria", variable=FC_BASE, timestamp=base, value=6.15))
    for h, lv, q in zip((12, 18, 42), levels, (2100.0, 2130.0, 2210.0)):
        t = base.replace(hour=0) + pd.Timedelta(hours=12 + h)
        obs += [Observation(source="copel_mhb", site="uniao_da_vitoria", variable=v, timestamp=t, value=x)
                for v, x in ((FC_LEVEL, lv), (FC_QOBS, q), (FC_LEVEL_DRY, lv), (FC_QOBS_DRY, q))]
    return obs


def test_forecast_reaches_the_report_and_the_panel():
    from iguacu_monitor import panel
    rep = _build(_uv_with_forecast())
    fc = rep.forecasts["uniao_da_vitoria"]
    assert fc.peak.level == 6.51 and fc.rising_at_end and not fc.dry_differs
    st = _state(rep, "uniao_da_vitoria")
    assert st.values == {QOBS: 2057.0, LEVEL: 6.15}            # el pronóstico no se cuela como dato
    page = panel.render(rep, _cfg(), fragment=True)
    assert "Alto Iguazú · cabecera" in page and "Máximo del pronóstico" in page
    assert "Supera por 25 cm la crecida de sep 2026" in page
    summary = page[page.index('<ul class="summary">'):page.index("</ul>", page.index('<ul class="summary">'))]
    assert 'class="pill warn"><span class="k">União da Vitória · SIMEPAR' in summary


def test_forecast_below_reference_is_not_a_warning():
    from iguacu_monitor import panel
    page = panel.render(_build(_uv_with_forecast(levels=(6.18, 6.19, 6.20))), _cfg(), fragment=True)
    assert "queda 6 cm por debajo de la crecida de sep 2026" in page.lower()
    assert 'class="pill"><span class="k">União da Vitória · SIMEPAR' in page
