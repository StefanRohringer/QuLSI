"""Feiertage werden berechnet, nicht geraten (Fall: "Allerheiligen am 3. November")."""

from datetime import date

from qulsi_core import feiertage as f


def test_ostern():
    assert [f.ostersonntag(j) for j in (2024, 2025, 2026, 2027)] == \
        [date(2024, 3, 31), date(2025, 4, 20), date(2026, 4, 5), date(2027, 3, 28)]


def test_2026_bundesweit_und_regional():
    tage = {name: d for d, name, _ in f.feiertage_jahr(2026)}
    assert tage["Karfreitag"] == date(2026, 4, 3) and tage["Pfingstmontag"] == date(2026, 5, 25)
    assert tage["Fronleichnam"] == date(2026, 6, 4) and tage["Buß- und Bettag"] == date(2026, 11, 18)
    assert tage["Allerheiligen"] == date(2026, 11, 1) and tage["Reformationstag"] == date(2026, 10, 31)


def test_naechster_feiertag_ab_7_oktober():
    alle = f.naechste(date(2026, 10, 7), 3)
    assert [(x["name"], x["datum"]) for x in alle] == [("Reformationstag", "2026-10-31"), ("Allerheiligen", "2026-11-01"),
                                                      ("Buß- und Bettag", "2026-11-18")]
    assert alle[1]["wochentag"] == "Sonntag" and "Bayern" in alle[1]["gilt_in"]
    by = f.naechste(date(2026, 10, 7), 2, "Bayern")
    assert [x["name"] for x in by] == ["Allerheiligen", "1. Weihnachtsfeiertag"]
    assert [x["name"] for x in f.naechste(date(2026, 10, 7), 1, "be")] == ["1. Weihnachtsfeiertag"]
    assert f.naechste(date(2026, 12, 27), 1, "HH")[0]["datum"] == "2027-01-01"  # über den Jahreswechsel
