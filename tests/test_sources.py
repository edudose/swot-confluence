"""Parseo de las respuestas de ANA y ONS, con muestras sintéticas."""

import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from iguacu_monitor import config
from iguacu_monitor.sources.ana import ANA, BRASILIA
from iguacu_monitor.sources.ons import ONS

# ANA entrega la hora de Brasilia (UTC-3) sin declararlo en el XML.
ANA_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<DataTable>
  <DocumentElement>
    <DadosHidrometereologicos>
      <CodEstacao>65992502</CodEstacao>
      <DataHora>2026-10-24T09:00:00</DataHora>
      <Vazao>15120,5</Vazao>
      <Nivel>6,12</Nivel>
      <Chuva>0</Chuva>
    </DadosHidrometereologicos>
    <DadosHidrometereologicos>
      <CodEstacao>65992502</CodEstacao>
      <DataHora>2026-10-24T10:00:00</DataHora>
      <Vazao>15230,0</Vazao>
      <Nivel>6,18</Nivel>
    </DadosHidrometereologicos>
    <DadosHidrometereologicos>
      <CodEstacao>65992502</CodEstacao>
      <DataHora>2026-10-24T11:00:00</DataHora>
    </DadosHidrometereologicos>
  </DocumentElement>
</DataTable>"""


def _cfg():
    return config.load("config/iguacu.yml")


def _ana_nodes(payload: bytes):
    root = ET.fromstring(payload)
    return [n for n in root.iter()
            if n.tag.rsplit("}", 1)[-1].lower().startswith("dadoshidromet")]


def test_ana_parses_flow_level_and_rain():
    obs = ANA(_cfg())._parse_records(_ana_nodes(ANA_XML), "hotel_cataratas")
    by_var = {}
    for o in obs:
        by_var.setdefault(o.variable, []).append(o.value)
    assert by_var["qobs"] == [15120.5, 15230.0]
    assert by_var["level"] == [6.12, 6.18]
    assert by_var["precip"] == [0.0]


def test_ana_converts_brasilia_time_to_utc():
    obs = ANA(_cfg())._parse_records(_ana_nodes(ANA_XML), "hotel_cataratas")
    first = min(obs, key=lambda o: o.timestamp)
    assert first.timestamp == datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    assert first.timestamp.astimezone(BRASILIA).hour == 9


def test_ana_skips_records_without_values():
    # El tercer registro no trae ninguna medición: no debe generar filas.
    obs = ANA(_cfg())._parse_records(_ana_nodes(ANA_XML), "hotel_cataratas")
    stamps = {o.timestamp.hour for o in obs}
    assert 14 not in stamps  # 11:00 BRT = 14:00 UTC


def test_ana_handles_corrected_schema_typo():
    # ANA escribe "Hidrometereologicos"; si algún día lo corrigen a
    # "Hidrometeorologicos", el parseo debe seguir funcionando.
    fixed = ANA_XML.replace(b"Hidrometereologicos", b"Hidrometeorologicos")
    obs = ANA(_cfg())._parse_records(_ana_nodes(fixed), "hotel_cataratas")
    assert len(obs) >= 2


ONS_FRAME = pd.DataFrame({
    "din_instante": ["2026-10-20", "2026-10-21", "2026-10-20", "2026-10-21"],
    "nom_reservatorio": ["GOV. JOSE RICHA", "GOV. JOSE RICHA",
                         "SALTO SANTIAGO", "USINA AJENA"],
    "val_vazaoafluente": [11800.0, 12050.0, 9100.0, 5.0],
    "val_vazaodefluente": [11700.0, 11980.0, 9050.0, 5.0],
})


def test_ons_maps_plants_and_variables():
    obs, notes = ONS(_cfg())._parse(ONS_FRAME, date(2026, 10, 20), date(2026, 10, 21))
    caxias = [o for o in obs if o.site == "salto_caxias"]
    assert {o.variable for o in caxias} == {"qafl", "qdef"}
    assert sorted(o.value for o in caxias if o.variable == "qdef") == [11700.0, 11980.0]


def test_ons_ignores_plants_outside_the_basin():
    obs, _ = ONS(_cfg())._parse(ONS_FRAME, date(2026, 10, 20), date(2026, 10, 21))
    assert all(o.site in {p.key for p in _cfg().plants} for o in obs)


def test_ons_respects_the_date_window():
    obs, _ = ONS(_cfg())._parse(ONS_FRAME, date(2026, 10, 21), date(2026, 10, 21))
    assert {o.timestamp.date() for o in obs} == {date(2026, 10, 21)}


def test_ons_reports_unrecognised_columns():
    bad = ONS_FRAME.rename(columns={"val_vazaoafluente": "algo_raro",
                                    "val_vazaodefluente": "otro"})
    obs, notes = ONS(_cfg())._parse(bad, date(2026, 10, 20), date(2026, 10, 21))
    assert obs == []
    assert any("caudal" in n for n in notes)


def test_ons_timestamps_are_utc_aware():
    obs, _ = ONS(_cfg())._parse(ONS_FRAME, date(2026, 10, 20), date(2026, 10, 21))
    assert obs and all(o.timestamp.tzinfo is not None for o in obs)
