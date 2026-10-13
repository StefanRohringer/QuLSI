from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from qulsi_core.memory.zeitraum import frage_lesen, ort_normalisieren, zeitraum_lesen

JETZT = datetime(2026, 10, 7, 18, 0, tzinfo=ZoneInfo("Europe/Berlin"))  # Mittwoch


@pytest.mark.parametrize("frage, von, bis", [
    ("Was war letzte Woche?", date(2026, 9, 28), date(2026, 10, 4)),
    ("gestern beim Arzt", date(2026, 10, 6), date(2026, 10, 6)),
    ("vorgestern", date(2026, 10, 5), date(2026, 10, 5)),
    ("Fotos vom 3.10.", date(2026, 10, 3), date(2026, 10, 3)),
    ("Fotos vom 24.12.", date(2025, 12, 24), date(2025, 12, 24)),  # Zukunft -> letztes Jahr
    ("am 3. Oktober 2025", date(2025, 10, 3), date(2025, 10, 3)),
    ("2026-02-01", date(2026, 2, 1), date(2026, 2, 1)),
    ("im März", date(2026, 3, 1), date(2026, 3, 31)),
    ("im November", date(2025, 11, 1), date(2025, 11, 30)),
    ("Juli 2025 Italien", date(2025, 7, 1), date(2025, 7, 31)),
    ("seit März", date(2026, 3, 1), date(2026, 10, 7)),
    ("letzten Monat", date(2026, 9, 1), date(2026, 9, 30)),
    ("in den letzten 3 Tagen", date(2026, 10, 5), date(2026, 10, 7)),
    ("vor drei Tagen", date(2026, 10, 4), date(2026, 10, 4)),
    ("am Montag", date(2026, 10, 5), date(2026, 10, 5)),
    ("letzten Mittwoch", date(2026, 9, 30), date(2026, 9, 30)),
    ("letztes Wochenende", date(2026, 10, 3), date(2026, 10, 4)),
    ("an Weihnachten", date(2025, 12, 24), date(2025, 12, 26)),
    ("Urlaub 2024", date(2024, 1, 1), date(2024, 12, 31)),
])
def test_zeitraeume(frage, von, bis):
    z, _ = zeitraum_lesen(frage, JETZT)
    assert z is not None, frage
    assert (z.von.date(), z.bis.date()) == (von, date.fromordinal(bis.toordinal() + 1))


@pytest.mark.parametrize("frage", ["Was hat Jan gesagt?", "Was weiß ich über August?", "Kaffee mit Max"])
def test_keine_zeit(frage):
    assert zeitraum_lesen(frage, JETZT)[0] is None


def test_frage_mit_person_ort_und_rest():
    f = frage_lesen("Was habe ich letzte Woche mit Maxi in München gegessen?", JETZT,
                    {"max muster": "Max Muster", "maxi": "Max Muster", "anna": "Anna"}, {"münchen": "München"})
    assert f.personen == ["Max Muster"] and f.orte == ["München"] and f.zeitraum.text == "letzte Woche"
    assert f.stichworte == ["gegessen"]


def test_name_im_wort_zaehlt_nicht():
    f = frage_lesen("Maximal drei Annahmen", JETZT, {"max": "Max", "anna": "Anna"}, {})
    assert f.personen == []


def test_ort_normalisieren():
    assert ort_normalisieren("Café Luitpold, Brienner Str. 11, 80333 München") == "Café Luitpold"
    assert ort_normalisieren("https://zoom.us/j/123") == ""
