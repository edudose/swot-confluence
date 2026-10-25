"""Persistencia y lectura del estado de la cuenca."""

from datetime import datetime, timedelta, timezone

import pytest

from iguacu_monitor import config, report, store
from iguacu_monitor.model import (LEVEL, PRECIP, QAFL, QDEF, QOBS, QTUR, QVER,
                                  Observation)

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def _cfg():
    return config.load("config/iguacu.yml")


def _obs(site, var, value, hours_ago=0, source="copel_mhb"):
    return Observation(source=source, site=site, variable=var,
                       timestamp=NOW - timedelta(hours=hours_ago), value=value)


# --------------------------------------------------------------- store ----
def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "s.parquet"
    added = store.save([_obs("salto_caxias", QDEF, 11700.0)], path)
    assert added == 1
    frame = store.load(path)
    assert len(frame) == 1
    assert frame.iloc[0]["value"] == 11700.0
    assert frame.iloc[0]["unit"] == "m3/s"


def test_save_deduplicates_on_natural_key(tmp_path):
    path = tmp_path / "s.parquet"
    store.save([_obs("salto_caxias", QDEF, 11700.0)], path)
    # Misma clave (fuente, sitio, variable, instante) con valor corregido.
    added = store.save([_obs("salto_caxias", QDEF, 11750.0)], path)
    frame = store.load(path)
    assert added == 0, "no debe crecer: es la misma observación"
    assert len(frame) == 1
    assert frame.iloc[0]["value"] == 11750.0, "la lectura más reciente gana"


def test_save_appends_distinct_observations(tmp_path):
    path = tmp_path / "s.parquet"
    store.save([_obs("salto_caxias", QDEF, 11700.0)], path)
    store.save([_obs("salto_caxias", QDEF, 11800.0, hours_ago=1)], path)
    assert len(store.load(path)) == 2


def test_load_missing_file_returns_empty(tmp_path):
    assert store.load(tmp_path / "no-existe.parquet").empty


def test_save_empty_is_noop(tmp_path):
    path = tmp_path / "s.parquet"
    assert store.save([], path) == 0


# -------------------------------------------------------------- report ----
def _populated():
    return [
        _obs("foz_do_areia", QAFL, 2450.0), _obs("foz_do_areia", QTUR, 1380.0),
        _obs("foz_do_areia", QVER, 1100.0), _obs("foz_do_areia", QDEF, 2480.0),
        _obs("foz_do_areia", LEVEL, 741.20),
        _obs("salto_caxias", QDEF, 11700.0), _obs("salto_caxias", QVER, 10450.0),
        _obs("hotel_cataratas", QOBS, 15120.0, source="ana"),
        _obs("porto_capanema", QOBS, 14690.0, source="ana"),
        Observation(source="inpe_merge", site="basin", variable=PRECIP,
                    timestamp=NOW - timedelta(days=1), value=42.5),
        Observation(source="inpe_merge", site="basin", variable=PRECIP,
                    timestamp=NOW - timedelta(days=2), value=18.0),
        Observation(source="inpe_merge", site="basin", variable=PRECIP,
                    timestamp=NOW - timedelta(days=10), value=60.0),
    ]


def _frame(observations):
    import pandas as pd
    return pd.DataFrame([o.as_row() for o in observations])


def test_report_collects_latest_value_per_variable():
    rep = report.build(_frame(_populated()), _cfg(), now=NOW)
    areia = next(s for s in rep.plants if s.key == "foz_do_areia")
    assert areia.values[QDEF] == 2480.0
    assert areia.values[LEVEL] == 741.20


def test_report_accumulates_precipitation_by_window():
    rep = report.build(_frame(_populated()), _cfg(), now=NOW)
    assert rep.precip[1] == pytest.approx(42.5)
    assert rep.precip[3] == pytest.approx(60.5)    # 42,5 + 18
    assert rep.precip[30] == pytest.approx(120.5)  # + 60 de hace 10 días


def test_report_flags_spilling_plants():
    rep = report.build(_frame(_populated()), _cfg(), now=NOW)
    assert next(s for s in rep.plants if s.key == "salto_caxias").spilling
    assert {s.key for s in rep.spilling} == {"foz_do_areia", "salto_caxias"}
    assert "vertiendo: Foz do Areia, Salto Caxias" in report.render_text(rep)


def test_report_flags_stale_data():
    stale = [_obs("salto_caxias", QDEF, 11700.0, hours_ago=30)]
    rep = report.build(_frame(stale), _cfg(), now=NOW)
    state = next(s for s in rep.plants if s.key == "salto_caxias")
    assert state.stale
    assert any("30 h" in n for n in rep.notes)


def _series(site, var, values_by_hours_ago, source="ana"):
    return [_obs(site, var, v, hours_ago=h, source=source) for h, v in values_by_hours_ago]


def test_report_flags_implausible_cascade_with_travel_time():
    # Cataratas compara contra Capanema hace 11 h (viaje configurado).
    obs = (_series("porto_capanema", QOBS, [(h, 11700.0) for h in range(0, 14)])
           + _series("hotel_cataratas", QOBS, [(0, 4000.0)]))
    rep = report.build(_frame(obs), _cfg(), now=NOW)
    assert any("Cataratas" in n and "hace 11 h" in n for n in rep.notes)


def test_report_does_not_flag_a_flood_wave_in_transit():
    """Datos reales del 24-oct: la onda ya pasó por Capanema y no llegó a
    Cataratas. Comparar valores simultáneos daba una falsa alarma."""
    capanema = [(h, 8700.0) for h in range(11, 30)] + [(h, 8700 + (11 - h) * 180.0) for h in range(0, 11)]
    obs = (_series("porto_capanema", QOBS, capanema)
           + _series("hotel_cataratas", QOBS, [(h, 9100.0) for h in range(0, 30)]))
    rep = report.build(_frame(obs), _cfg(), now=NOW)
    assert not any("por debajo" in n for n in rep.notes)


def test_coherence_check_skips_when_history_is_too_short():
    obs = (_series("porto_capanema", QOBS, [(0, 11700.0), (1, 11600.0)])
           + _series("hotel_cataratas", QOBS, [(0, 4000.0)]))
    rep = report.build(_frame(obs), _cfg(), now=NOW)
    assert not any("Cataratas" in n for n in rep.notes)


def test_report_on_empty_store_explains_itself():
    import pandas as pd
    rep = report.build(pd.DataFrame(columns=["source", "site", "variable",
                                             "timestamp", "value", "unit"]),
                       _cfg(), now=NOW)
    assert rep.sites == []
    assert any("fetch" in n for n in rep.notes)


def test_render_text_includes_key_figures():
    text = report.render_text(report.build(_frame(_populated()), _cfg(), now=NOW))
    assert "Rio Iguaçu" in text
    assert "11,700" in text      # defluencia de Salto Caxias
    assert "15,120" in text      # caudal en Cataratas
    assert "REGULACIÓN" in text and "PRECIPITACIÓN" in text
