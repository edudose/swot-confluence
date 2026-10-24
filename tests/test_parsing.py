"""Parseo de números y etiquetas: lo que rompe silenciosamente si falla."""

import pytest

from iguacu_monitor.model import match_variable, normalize_label
from iguacu_monitor.tables import parse_number


@pytest.mark.parametrize("raw,expected", [
    ("1.234,5", 1234.5),      # portugués: punto de miles, coma decimal
    ("1,234.5", 1234.5),      # inglés
    ("11700", 11700.0),
    ("10.030", 10030.0),      # miles sin decimales
    ("742,15", 742.15),
    ("0,5", 0.5),
    ("3.8", 3.8),             # decimal con punto
    ("11.700,25", 11700.25),
    ("  92,0 %", 92.0),
    ("1 234,5", 1234.5),      # espacio como separador de miles
    ("-5,5", -5.5),
])
def test_parse_number_locales(raw, expected):
    assert parse_number(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["", "-", "--", "n/a", "s/d", None, "sin dato"])
def test_parse_number_missing(raw):
    assert parse_number(raw) is None


@pytest.mark.parametrize("label,var", [
    ("Vazão Afluente (m³/s)", "qafl"),
    ("Vazão Defluente", "qdef"),
    ("Vazão Turbinada", "qtur"),
    ("Vazão Vertida", "qver"),
    ("Vazao", "qobs"),
    ("Nível Montante (m)", "level"),
    ("Cota", "level"),
    ("Volume Útil (%)", "storage"),
    ("Chuva (mm)", "precip"),
])
def test_match_variable(label, var):
    assert match_variable(label) == var


def test_match_variable_prefers_longest_alias():
    # "vazao defluente" debe ganarle a "vazao", que también está contenido.
    assert match_variable("Vazão Defluente (m³/s)") == "qdef"


def test_match_variable_unknown():
    assert match_variable("Potência Instalada") is None


def test_normalize_label_strips_accents():
    assert normalize_label("Vazão Defluente (m³/s)") == "vazao defluente m3 s"
