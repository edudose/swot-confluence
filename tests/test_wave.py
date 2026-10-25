"""Onda hacia Cataratas: traslado de lo observado por tiempo de viaje."""

from datetime import datetime, timedelta, timezone

import math
import pandas as pd
import pytest

from iguacu_monitor import config, panel, report
from iguacu_monitor.model import QDEF, QOBS, Observation
from iguacu_monitor.report import estimate_lag

NOW = datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc)   # sáb 23:00 local


def _cfg():
    return config.load("config/iguacu.yml")


def _hydrograph(h_ago: int, onset_h_ago: int = 8, base: float = 8700, rate: float = 400) -> float:
    """Meseta y luego ascenso lineal desde `onset_h_ago` horas atrás."""
    return base if h_ago >= onset_h_ago else base + (onset_h_ago - h_ago) * rate


def _obs(site, var, fn, hours=48, source="t"):
    return [Observation(source=source, site=site, variable=var,
                        timestamp=NOW - timedelta(hours=h), value=fn(h)) for h in range(hours)]


def _frame(obs):
    return pd.DataFrame([o.as_row() for o in obs])


def _scenario(lag=10, onset=8):
    cap = lambda h: _hydrograph(h, onset)                             # noqa: E731
    cat = lambda h: _hydrograph(h + lag, onset)                       # Cataratas = Capanema desfasado
    obs = (_obs("porto_capanema", QOBS, cap) + _obs("hotel_cataratas", QOBS, cat)
           + _obs("baixo_iguacu", QDEF, lambda h: 15000.0) + _obs("salto_caxias", QDEF, lambda h: 15200.0))
    return report.build(_frame(obs), _cfg(), now=NOW)


def test_arrivals_follow_configured_travel_times():
    w = _scenario().wave
    assert [a.name for a in w.arrivals] == ["Porto Capanema", "Baixo Iguaçu"]
    assert [a.lag_h for a in w.arrivals] == [11, 15]          # 11; 4 + 11
    cap = w.arrivals[0]
    assert cap.eta == cap.seen + timedelta(hours=11)


def test_travel_times_accumulate_further_upstream():
    rep = _scenario()
    w = report.wave_to("hotel_cataratas", rep.sites, _cfg(), max_points=3)
    assert [a.lag_h for a in w.arrivals] == [11, 15, 16]      # Caxias: 1 + 4 + 11


def test_lag_is_estimated_from_the_data():
    # El ascenso tiene que verse en las dos series para poder correlacionarlas.
    w = _scenario(lag=10, onset=20).wave
    assert w.lag_est_h == 10 and w.lag_r >= 0.99
    assert w.arrivals[0].eta_alt == w.arrivals[0].seen + timedelta(hours=10)


def test_rise_onset_is_the_end_of_the_plateau():
    w = _scenario().wave
    assert w.rising
    assert w.front_seen == NOW - timedelta(hours=8)   # último dato de la meseta
    assert w.rise_q == pytest.approx(8 * 400)         # lo que subió desde entonces


def test_lag_is_not_estimated_before_the_wave_reaches_the_target():
    # Con el ascenso todavía en camino, Cataratas está plana: no hay desfase
    # que estimar y se usa el configurado.
    w = _scenario(lag=10, onset=8).wave
    assert w.lag_est_h is None and w.arrivals[0].eta_alt is None


def test_no_rise_detected_on_flat_flow():
    obs = (_obs("porto_capanema", QOBS, lambda h: 8700.0) + _obs("hotel_cataratas", QOBS, lambda h: 9000.0)
           + _obs("baixo_iguacu", QDEF, lambda h: 8800.0))
    w = report.build(_frame(obs), _cfg(), now=NOW).wave
    assert not w.rising
    assert w.lag_est_h is None                        # desfase indefinido con caudal plano


def test_estimate_lag_rejects_weak_correlation():
    t0 = NOW - timedelta(hours=60)
    up = [(t0 + timedelta(hours=h), 9000 + 900 * math.sin(h / 5)) for h in range(60)]
    down = [(t0 + timedelta(hours=h), 9000 + 900 * math.sin(h * 1.7)) for h in range(60)]
    assert estimate_lag(up, down, around_h=11) == (None, None)


def test_panel_shows_the_wave_section_and_pill():
    cfg = _cfg()
    page = panel.render(_scenario(), cfg, fragment=True)
    assert "Onda hacia Cataratas" in page
    assert "Empieza a subir" in page
    assert "Llega lo que sale de Baixo" in page
    assert 'class="pill warn"><span class="k">Onda a Cataratas' in page
    assert "No incluye lluvia futura" in page


@pytest.mark.parametrize("a,b,text", [
    (datetime(2026, 10, 25, 12, tzinfo=timezone.utc), datetime(2026, 10, 25, 13, tzinfo=timezone.utc), "dom 09–10 h"),
    (datetime(2026, 10, 25, 2, tzinfo=timezone.utc), datetime(2026, 10, 25, 3, tzinfo=timezone.utc), "sáb 23 h – dom 00 h"),
    (datetime(2026, 10, 25, 12, tzinfo=timezone.utc), None, "dom 09 h"),
])
def test_time_ranges_in_local_time(a, b, text):
    assert panel._when_range(a, b, timezone(timedelta(hours=-3))) == text
