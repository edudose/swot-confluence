"""Extracción desde tablas HTML al estilo de la página MHB de COPEL."""

from datetime import datetime, timezone
from iguacu_monitor import config, tables

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)

# Imita la forma esperada de la página: una tabla de usinas y otra de
# estaciones, con encabezados en portugués y números en formato brasileño.
HTML = """
<html><body>
<table>
  <tr><th>Usina</th><th>Vazão Afluente (m³/s)</th><th>Vazão Turbinada (m³/s)</th>
      <th>Vazão Vertida (m³/s)</th><th>Vazão Defluente (m³/s)</th><th>Nível (m)</th></tr>
  <tr><td>FOZ DO AREIA</td><td>2.450,0</td><td>1.380,5</td><td>1.100,0</td>
      <td>2.480,5</td><td>741,20</td></tr>
  <tr><td>SEGREDO</td><td>2.600,0</td><td>1.240,0</td><td>1.500,0</td>
      <td>2.740,0</td><td>606,80</td></tr>
  <tr><td>SALTO CAXIAS</td><td>11.800,0</td><td>1.250,0</td><td>10.450,0</td>
      <td>11.700,0</td><td>324,50</td></tr>
</table>
<table>
  <tr><th>Estação</th><th>Vazão (m³/s)</th><th>Nível (m)</th></tr>
  <tr><td>PORTO CAPANEMA</td><td>14.690,0</td><td>8,45</td></tr>
  <tr><td>HOTEL CATARATAS</td><td>15.120,0</td><td>6,12</td></tr>
</table>
</body></html>
"""


def _cfg():
    return config.load("config/iguacu.yml")


def _frames():
    return tables.read_tables(HTML)


def test_extracts_plants_and_stations():
    cfg = _cfg()
    obs, notes = tables.extract(_frames(), cfg.sites, source="copel_mhb", timestamp=NOW)

    got = {(o.site, o.variable): o.value for o in obs}
    assert got[("foz_do_areia", "qafl")] == 2450.0
    assert got[("foz_do_areia", "qdef")] == 2480.5
    assert got[("segredo", "qver")] == 1500.0
    assert got[("salto_caxias", "qdef")] == 11700.0
    assert got[("salto_caxias", "level")] == 324.50
    assert got[("porto_capanema", "qobs")] == 14690.0
    assert got[("hotel_cataratas", "qobs")] == 15120.0


def test_sites_without_copel_label_are_not_expected():
    # Un sitio con copel_label nulo no se busca en el HTML, así que su
    # ausencia no debe reportarse como falta.
    from dataclasses import replace

    sites = [replace(s, copel_label=None) if s.key == "salto_santiago" else s for s in _cfg().sites]
    _, notes = tables.extract(_frames(), sites, source="copel_mhb", timestamp=NOW)
    assert not any("salto_santiago" in n for n in notes)


def test_missing_labelled_site_is_reported():
    # Si un sitio SÍ tiene etiqueta COPEL y aun así no aparece, hay que
    # avisarlo: devolver vacío en silencio es el modo de falla peligroso.
    cfg = _cfg()
    sin_cataratas = HTML.replace("HOTEL CATARATAS", "OTRA ESTACION")
    obs, notes = tables.extract(
        tables.read_tables(sin_cataratas), cfg.sites,
        source="copel_mhb", timestamp=NOW,
    )
    assert any("hotel_cataratas" in n for n in notes)


def test_brazilian_decimals_are_not_corrupted():
    """Regresión: pandas.read_html convertía "741,20" en 74120.

    Es el fallo más peligroso de este parseo, porque no lanza error: entrega
    un nivel dos órdenes de magnitud mayor y el monitor lo reporta como si
    nada. Se cubre explícitamente para que nadie reintroduzca read_html.
    """
    cfg = _cfg()
    obs, _ = tables.extract(_frames(), cfg.sites, source="copel_mhb", timestamp=NOW)
    niveles = {o.site: o.value for o in obs if o.variable == "level"}
    assert niveles["foz_do_areia"] == 741.20
    assert niveles["segredo"] == 606.80
    assert niveles["hotel_cataratas"] == 6.12


def test_units_are_assigned():
    cfg = _cfg()
    obs, _ = tables.extract(_frames(), cfg.sites, source="copel_mhb", timestamp=NOW)
    flows = [o for o in obs if o.variable == "qdef"]
    assert flows and all(o.unit == "m3/s" for o in flows)
    levels = [o for o in obs if o.variable == "level"]
    assert levels and all(o.unit == "m" for o in levels)


def test_empty_html_yields_nothing():
    obs, _ = tables.extract([], _cfg().sites, source="copel_mhb", timestamp=NOW)
    assert obs == []


def test_timestamps_are_timezone_aware():
    cfg = _cfg()
    obs, _ = tables.extract(_frames(), cfg.sites, source="copel_mhb", timestamp=NOW)
    assert all(o.timestamp.tzinfo is not None for o in obs)
