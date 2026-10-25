"""Estado de las usinas y panel HTML de la cascada."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from iguacu_monitor import config, demo, panel, report
from iguacu_monitor.model import QAFL, QDEF, Observation
from iguacu_monitor.report import ALERTA, ATENCION, NIVEL, NORMAL, SIN_DATO, slope_per_hour

T = datetime(2026, 10, 24, 1, 0, tzinfo=timezone.utc)


def _cfg():
    return config.load("config/iguacu.yml")


def _rep(observations, now=T):
    frame = pd.DataFrame([o.as_row() for o in observations])
    return report.build(frame, _cfg(), now=now)


def _inflow(site, value):
    return Observation(source="t", site=site, variable=QAFL, timestamp=T, value=value)


def _state(rep, key):
    return next(s for s in rep.sites if s.key == key)


# -------------------------------------------- estado por caudal afluente ----
@pytest.mark.parametrize("inflow,expected", [
    (2999, NORMAL), (3000, ATENCION), (4999, ATENCION), (5000, ALERTA), (9000, ALERTA),
])
def test_caxias_thresholds_on_inflow(inflow, expected):
    # Caxias: atención >= 3.000, alerta >= 5.000 (IO-OR.PR.IGU Rev.44).
    assert _state(_rep([_inflow("salto_caxias", inflow)]), "salto_caxias").status == expected


@pytest.mark.parametrize("inflow,expected", [
    (3999, NORMAL), (4000, ATENCION), (6999, ATENCION), (7000, ALERTA),
])
def test_santiago_thresholds_on_inflow(inflow, expected):
    assert _state(_rep([_inflow("salto_santiago", inflow)]), "salto_santiago").status == expected


def test_status_uses_inflow_not_outflow():
    # Entra poco, sale mucho: manda lo que entra.
    obs = [_inflow("salto_caxias", 2500),
           Observation(source="t", site="salto_caxias", variable=QDEF, timestamp=T, value=9000)]
    assert _state(_rep(obs), "salto_caxias").status == NORMAL


def test_level_operated_plants_ignore_flow():
    assert _state(_rep([_inflow("foz_do_areia", 99999)]), "foz_do_areia").status == NIVEL


def test_missing_inflow_is_reported_as_no_data():
    obs = [Observation(source="t", site="salto_caxias", variable=QDEF, timestamp=T, value=7000)]
    assert _state(_rep(obs), "salto_caxias").status == SIN_DATO


def test_stations_have_no_status():
    rep = _rep(demo.example_observations(), now=demo.SNAPSHOT)
    assert _state(rep, "porto_capanema").status is None


def test_config_rejects_inverted_thresholds(tmp_path):
    raw = open("config/iguacu.yml", encoding="utf-8").read()
    bad = raw.replace("attention: 3000\n    alert: 5000", "attention: 5000\n    alert: 3000", 1)
    path = tmp_path / "bad.yml"
    path.write_text(bad, encoding="utf-8")
    with pytest.raises(ValueError, match="alerta"):
        config.load(path)


# ------------------------------------------------------------- tendencia ----
def test_slope_is_exact_on_a_line():
    pts = [(T - timedelta(hours=h), 1000 + 50 * (3 - h)) for h in (3, 2, 1, 0)]
    assert slope_per_hour(pts) == pytest.approx(50)


def test_slope_handles_irregular_sampling():
    pts = [(T - timedelta(minutes=m), 2000 - m) for m in (170, 95, 40, 0)]
    assert slope_per_hour(pts) == pytest.approx(60)  # +1 m3/s por minuto


def test_slope_needs_two_points():
    assert slope_per_hour([(T, 1.0)]) is None


def test_demo_reproduces_dashboard_states_and_trends():
    """Los ocho estados y tendencias de la captura del 23-oct 22:00."""
    rep = _rep(demo.example_observations(), now=demo.SNAPSHOT)
    band = _cfg().report["trend_flat_band"]
    expected = {
        "foz_do_areia": (NIVEL, 32, "up"), "segredo": (NIVEL, 11, "flat"),
        "salto_santiago": (ATENCION, 16, "up"), "salto_osorio": (ATENCION, 27, "up"),
        "salto_caxias": (ALERTA, -71, "down"), "baixo_iguacu": (ALERTA, 137, "up"),
    }
    for key, (status, trend, direction) in expected.items():
        state = _state(rep, key)
        assert state.status == status, key
        assert state.trend_per_h == pytest.approx(trend), key
        assert state.trend_direction(band) == direction, key


def test_station_peak_matches_dashboard():
    # Tablero: "máximo 7.253, el 23 · 22 h" (hora local).
    rep = _rep(demo.example_observations(), now=demo.SNAPSHOT)
    when, value = _state(rep, "hotel_cataratas").peak
    assert value == pytest.approx(7253)
    assert when == demo.SNAPSHOT


def test_peak_ties_resolve_to_latest():
    obs = [Observation(source="t", site="porto_capanema", variable="qobs",
                       timestamp=T - timedelta(hours=h), value=v)
           for h, v in ((5, 9000), (3, 8000), (1, 9000))]
    when, value = _state(_rep(obs), "porto_capanema").peak
    assert value == 9000 and when == T - timedelta(hours=1)


# ----------------------------------------------------------------- panel ----
@pytest.fixture(scope="module")
def html():
    rep = _rep(demo.example_observations(), now=demo.SNAPSHOT)
    return panel.render(rep, _cfg(), note=demo.NOTE, fragment=True)


def test_panel_shows_cards_in_cascade_order(html):
    names = ["Foz do Areia", "Segredo", "Salto Santiago", "Salto Osório",
             "Salto Caxias", "Baixo Iguaçu", "Capanema", "Cataratas"]
    positions = [html.index(f"<h2>{n}</h2>") for n in names]
    assert positions == sorted(positions)


def test_panel_chips_follow_status(html):
    assert html.count("Opera por nivel") == 2
    assert html.count("Atención ≥ 4.000") == 2
    assert html.count("Alerta ≥ 5.000") == 2
    assert html.count('class="card is-alerta"') == 2


def test_panel_uses_southern_cone_number_format(html):
    assert "7.844" in html and "737,6 m" in html and "−71/h" in html


def test_panel_shows_ana_codes_and_travel_times(html):
    assert "Tronco 65987000" in html and "Tronco 65992500" in html
    assert "≈ 4 h" in html and "≈ 11 h" in html


def test_panel_labels_example_data(html):
    assert html.count("Datos de ejemplo") == 1 and "sintéticas" in html


def test_panel_station_peak_time(html):
    assert "23 · 22 h" in html


def test_panel_clock_uses_local_time(html):
    assert ">22:00<" in html and "vie 23 oct 2026" in html


def test_panel_header_credits_the_processing_team(html):
    header = html[html.index('<header class="top">'):html.index("</header>")]
    assert "Procesado por: Sector Hidrología / D.T EBY" in header


def test_fragment_has_no_document_skeleton(html):
    assert "<html" not in html and "<body" not in html and "<!doctype" not in html.lower()
    assert "<title>Cascada del Iguazú</title>" in html


def test_document_mode_is_complete():
    rep = _rep(demo.example_observations(), now=demo.SNAPSHOT)
    doc = panel.render(rep, _cfg())
    assert doc.startswith("<!doctype html>") and doc.rstrip().endswith("</html>")
    assert "Datos de ejemplo" not in doc


def test_panel_without_data_renders_dashes():
    rep = _rep([_inflow("salto_caxias", 4200)])
    page = panel.render(rep, _cfg(), fragment=True)
    assert "Atención ≥ 3.000" in page
    assert "—" in page


def test_text_is_escaped():
    cfg = _cfg()
    cfg.site("segredo").name = "Segredo <script>"
    rep = report.build(pd.DataFrame([_inflow("segredo", 1).as_row()]), cfg, now=T)
    assert "<script>" not in panel.render(rep, cfg, fragment=True)


@pytest.mark.parametrize("value,text", [
    (8978, "8.978"), (737.6, "737,6"), (1234567, "1.234.567"), (None, "—"),
])
def test_number_formatting(value, text):
    out = panel.fmt_dec(value) if isinstance(value, float) else panel.fmt_int(value)
    assert out == text


@pytest.mark.parametrize("raw,shown", [
    ("CGH CAVERNOSO BARRAMENTO", "CGH Cavernoso Barramento"),
    ("UHE GOV. NEY AMINTHAS BARRAMENTO", "UHE Gov. Ney Aminthas Barramento"),
    ("PCH SÃO LUIZ II", "PCH São Luiz II"),
])
def test_station_title_keeps_acronyms(raw, shown):
    assert panel.station_title(raw) == shown
