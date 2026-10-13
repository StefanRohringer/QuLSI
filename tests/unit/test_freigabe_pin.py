"""Freigabe-PIN für Erlauben in der App: Hash, Sperre nach Fehlversuchen, Zustand übersteht Neustart."""

import asyncio
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from qulsi_core.confirm import AppPin, PinFehler, pin_hash, pin_stimmt

ROOT = Path(__file__).resolve().parents[2]


class FakeStore:
    """Nur system_state, genug für AppPin."""

    def __init__(self):
        self.state: dict[str, dict] = {}

    async def eine(self, sql, params=None):
        v = self.state.get(params[0])
        return {"value": v} if v is not None else None

    async def conn(self):
        store = self

        class C:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def execute(self, sql, params):
                if sql.lstrip().startswith("DELETE"):
                    store.state.pop(params[0], None)
                else:
                    store.state[params[0]] = json.loads(params[1])

        return C()


def lauf(coro):
    return asyncio.run(coro)


def test_hash_und_klartext():
    h = pin_hash("246810")
    assert re.fullmatch(r"scrypt:[0-9a-f]{32}:[0-9a-f]{64}", h) and "$" not in h
    assert pin_stimmt(h, "246810") and not pin_stimmt(h, "246811")
    assert pin_stimmt("135790", "135790") and not pin_stimmt("135790", "13579")
    assert not pin_stimmt("scrypt:kaputt", "246810")


def test_ohne_pin_kein_erlauben_in_der_app():
    with pytest.raises(PinFehler) as f:
        lauf(AppPin(FakeStore(), "").pruefen("246810"))
    assert f.value.status == 403 and "Einrichtungscode" in f.value.text


def test_sperre_nach_fuenf_fehlversuchen_und_neustart():
    store = FakeStore()
    pin = AppPin(store, pin_hash("246810"))
    lauf(pin.pruefen("246810"))
    for uebrig in (4, 3, 2, 1):
        with pytest.raises(PinFehler) as f:
            lauf(pin.pruefen("000000"))
        assert f.value.status == 403 and f.value.daten["versuche_uebrig"] == uebrig
    with pytest.raises(PinFehler) as f:
        lauf(pin.pruefen("000000"))
    assert f.value.status == 429 and f.value.daten["neu_gesperrt"]
    # Gesperrt: auch die richtige PIN geht nicht, auch nicht nach einem Neustart
    neu = AppPin(store, pin.gespeichert)
    with pytest.raises(PinFehler) as f:
        lauf(neu.pruefen("246810"))
    assert f.value.status == 429 and "neu_gesperrt" not in f.value.daten
    # Sperre vorbei: richtige PIN geht und setzt alles zurück; die nächste Sperre wäre doppelt so lang
    assert store.state["freigabe_pin"]["sperren"] == 1
    store.state["freigabe_pin"]["gesperrt_bis"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    lauf(neu.pruefen("246810"))
    assert store.state["freigabe_pin"] == {"fehler": 0, "sperren": 0, "gesperrt_bis": None}


def test_zweite_sperre_doppelt_so_lang():
    store = FakeStore()
    store.state["freigabe_pin"] = {"fehler": 4, "sperren": 1, "gesperrt_bis": None}
    with pytest.raises(PinFehler):
        lauf(AppPin(store, pin_hash("246810")).pruefen("1"))
    bis = datetime.fromisoformat(store.state["freigabe_pin"]["gesperrt_bis"])
    assert timedelta(minutes=29) < bis - datetime.now(timezone.utc) <= timedelta(minutes=30)


def test_leere_und_ueberlange_pin_zaehlen_als_fehler():
    store = FakeStore()
    pin = AppPin(store, pin_hash("246810"))
    for falsch in ("", "1" * 65):
        with pytest.raises(PinFehler):
            lauf(pin.pruefen(falsch))
    assert store.state["freigabe_pin"]["fehler"] == 2


def test_alte_pin_aus_env_wird_uebernommen():
    store = FakeStore()
    pin = AppPin(store, "135790")  # Klartext aus einer alten RunPod-env
    lauf(pin.laden())
    assert store.state["freigabe_pin_hash"]["hash"].startswith("scrypt:") and pin_stimmt(pin.gespeichert, "135790")
    # Danach gilt die Datenbank, auch wenn die env später etwas anderes sagt
    lauf(pin.setzen("246810", "app_alte_pin"))
    neu = AppPin(store, "135790")
    lauf(neu.laden())
    assert pin_stimmt(neu.gespeichert, "246810") and not pin_stimmt(neu.gespeichert, "135790")


def test_pin_regeln():
    assert AppPin.pin_gueltig("246810") and AppPin.pin_gueltig("1" * 12)
    for falsch in ("12345", "1" * 13, "12345a", "١٢٣٤٥٦", ""):
        assert not AppPin.pin_gueltig(falsch)
    with pytest.raises(PinFehler) as f:
        lauf(AppPin(FakeStore(), "").setzen("123", "app"))
    assert f.value.status == 400


def test_einrichtungscode_einmal_und_nur_kurz():
    store = FakeStore()
    pin = AppPin(store, "")
    code = lauf(pin.code_neu())
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}", code) and code.replace("-", "") not in json.dumps(store.state)
    assert lauf(pin.code_offen())
    lauf(pin.code_einloesen(code.lower().replace("-", " ")))  # Schreibweise egal
    with pytest.raises(PinFehler) as f:
        lauf(pin.code_einloesen(code))  # verbraucht
    assert f.value.status == 403 and not lauf(pin.code_offen())
    code = lauf(pin.code_neu())
    store.state["freigabe_pin_code"]["bis"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with pytest.raises(PinFehler):
        lauf(pin.code_einloesen(code))  # abgelaufen


def test_falsche_codes_machen_den_code_ungueltig():
    store = FakeStore()
    pin = AppPin(store, "")
    code = lauf(pin.code_neu())
    for uebrig in (4, 3, 2, 1):
        with pytest.raises(PinFehler) as f:
            lauf(pin.code_einloesen("AAAA-AAAA"))
        assert f.value.daten["versuche_uebrig"] == uebrig
    with pytest.raises(PinFehler) as f:
        lauf(pin.code_einloesen("AAAA-AAAA"))
    assert f.value.status == 429
    with pytest.raises(PinFehler):
        lauf(pin.code_einloesen(code))  # auch der richtige gilt jetzt nicht mehr


def test_neue_pin_setzt_sperre_zurueck_und_entfernen():
    store = FakeStore()
    store.state["freigabe_pin"] = {"fehler": 0, "sperren": 2, "gesperrt_bis": "2999-01-01T00:00:00+00:00"}
    pin = AppPin(store, "")
    lauf(pin.setzen("246810", "app_einrichtungscode"))
    lauf(pin.pruefen("246810"))
    lauf(pin.setzen(None, "box"))
    assert not pin.eingerichtet and store.state["freigabe_pin_hash"]["hash"] == ""
    alt = AppPin(store, pin_hash("135790"))  # entfernt bleibt entfernt, auch mit PIN in der env
    lauf(alt.laden())
    assert not alt.eingerichtet
