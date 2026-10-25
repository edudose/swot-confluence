"""Capa de subcuencas, promedios areales e interpolación de pluviómetros."""

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from iguacu_monitor import config, panel, report, subbasins
from iguacu_monitor.model import PRECIP, Observation

NOW = datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)
LAYER = "config/subcuencas_iguacu.geojson"


def _cfg():
    return config.load("config/iguacu.yml")


def _n():
    return len(_cfg().subbasins_opts["outlets"])


def test_layer_follows_the_configured_cut_points():
    subs = subbasins.load(LAYER)
    assert [s.id for s in subs] == _cfg().subbasins_opts["outlets"]
    assert sum(s.area_km2 for s in subs) == pytest.approx(67_223, rel=0.01)


def test_layer_validation_against_ana_is_recorded():
    """Cada polígono guarda su error de área de drenaje contra ANA."""
    data = json.load(open(LAYER, encoding="utf-8"))
    errors = [f["properties"]["drainage_error"] for f in data["features"]]
    assert len(errors) == _n() and max(abs(e) for e in errors) <= 0.005
    assert data["metadata"]["max_area_error_vs_ana"] <= 0.005


def test_areal_means_of_a_constant_field():
    subs = subbasins.load(LAYER)
    lons = np.arange(-54.95, -48.0, 0.1)
    lats = np.arange(-26.95, -24.0, 0.1)
    means = subbasins.areal_means(np.full((len(lats), len(lons)), 7.0), lons, lats, subs)
    assert means["basin"] == pytest.approx(7.0)
    assert all(means[s.key] == pytest.approx(7.0) for s in subs)


def test_areal_means_follow_the_field():
    # Lluvia que crece hacia el este: la cabecera debe recibir más que Cataratas.
    subs = subbasins.load(LAYER)
    lons = np.arange(-54.95, -48.0, 0.1)
    lats = np.arange(-26.95, -24.0, 0.1)
    field = np.tile(lons + 60.0, (len(lats), 1))
    means = subbasins.areal_means(field, lons, lats, subs)
    assert means["sb_foz_do_areia"] > means["sb_salto_caxias"] > means["sb_hotel_cataratas"]


def test_non_regulating_station_is_not_a_cut_point():
    """União da Vitória no regula: su cuenca va dentro del área de Foz do Areia."""
    ids = [s.id for s in subbasins.load(LAYER)]
    assert "uniao_da_vitoria" not in ids
    foz = next(s for s in subbasins.load(LAYER) if s.id == "foz_do_areia")
    assert foz.area_km2 == pytest.approx(30_144, rel=0.01)


def test_idw_reproduces_points_and_constants():
    lons, lats = np.array([0.0, 0.5, 1.0]), np.array([0.0, 0.5])
    grid = subbasins.idw_grid([0.0, 1.0], [0.0, 0.5], [10.0, 30.0], lons, lats)
    assert grid[0, 0] == pytest.approx(10.0) and grid[1, 2] == pytest.approx(30.0)
    flat = subbasins.idw_grid([0.0, 1.0, 0.5], [0.0, 0.5, 0.2], [5.0, 5.0, 5.0], lons, lats)
    assert np.allclose(flat, 5.0)


def _uniform_gauge_rain(mm_per_hour: float, hours: int = 30):
    obs = []
    for g in _cfg().gauges:
        for h in range(hours):
            obs.append(Observation(source="ana", site=g.key, variable=PRECIP,
                                   timestamp=NOW - timedelta(hours=h), value=mm_per_hour))
    return pd.DataFrame([o.as_row() for o in obs])


def test_uniform_rain_gives_uniform_subbasins():
    rep = report.build(_uniform_gauge_rain(1.0), _cfg(), now=NOW)
    assert len(rep.subbasins) == _n()
    for sb in rep.subbasins:
        assert sb.gauge_mm[24] == pytest.approx(24.0, rel=1e-6)
        assert sb.gauge_mm[6] == pytest.approx(6.0, rel=1e-6)


def test_gauge_counts_per_subbasin_are_consistent():
    rep = report.build(_uniform_gauge_rain(1.0), _cfg(), now=NOW)
    assert sum(sb.n_gauges for sb in rep.subbasins) == rep.gauges[24].n


def test_merge_values_per_subbasin_are_reported():
    frame = pd.DataFrame([Observation(source="inpe_merge", site="sb_segredo", variable=PRECIP,
                                      timestamp=NOW - timedelta(hours=13), value=38.1).as_row()])
    rep = report.build(frame, _cfg(), now=NOW)
    seg = next(sb for sb in rep.subbasins if sb.id == "segredo")
    assert seg.merge_mm[1] == pytest.approx(38.1)


def test_panel_draws_the_rain_map():
    cfg = _cfg()
    page = panel.render(report.build(_uniform_gauge_rain(1.0), cfg, now=NOW), cfg, fragment=True)
    assert "Lluvia por subcuenca" in page
    assert page.count('class="sb"') == _n()
    assert page.count('class="outlet"') == _n()
    assert "interpolar los vecinos" in page          # Cataratas no tiene pluviómetros
    assert "Procesado por: Sector Hidrología / D.T EBY" in page


@pytest.mark.parametrize("mm,color", [(0, "#0d366b"), (24.9, "#184f95"), (101, "#9ec5f4"), (500, "#cde2fb")])
def test_rain_ramp_classes(mm, color):
    assert panel.rain_color(mm) == color
