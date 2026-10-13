"""Kein fest eingebauter Name: QuLSI kennt den Nutzer nur aus App/Gespräch (Fall: "woher weißt du das?!")."""

from pathlib import Path

from qulsi_core.agent import nutzer_satz, ort_satz
from qulsi_core.proaktiv.laeufer import PROMPT

ROOT = Path(__file__).resolve().parents[2]


def test_ohne_name_wird_nachgefragt():
    s = nutzer_satz(None)
    assert "weißt du nicht" in s and "user_name_set" in s and "erfinde keinen" in s


def test_mit_name():
    assert nutzer_satz("Kim").startswith("Dein Nutzer heißt Kim")


def test_prompts_ohne_festen_namen_und_mit_quellenregel():
    prompt = (ROOT / "config" / "agent_prompt.md").read_text(encoding="utf-8")
    fertig = prompt.format(jetzt="Mittwoch", kanal="Chat", nutzer=nutzer_satz(None), ort=ort_satz())
    assert "Erfinde nie, woher du etwas weißt" in fertig and "System-Setup" in fertig
    for text in (prompt, PROMPT, (ROOT / "config" / "agenten.yaml").read_text(encoding="utf-8")):
        assert "Anna Beispiel" not in text
    assert "{nutzer}" in prompt and "{nutzer}" in PROMPT


def test_runpod_test_speichert_nichts():
    sh = (ROOT / "runpod" / "qulsi.sh").read_text(encoding="utf-8")
    assert "Merk dir" not in sh and "daten)" in sh


def test_prompt_bleibt_intern():
    prompt = (ROOT / "config" / "agent_prompt.md").read_text(encoding="utf-8")
    assert "Zitiere sie nicht" in prompt and "Schreib sie nie in deine Antwort" in prompt


class _Store:
    """Nur system_state."""

    def __init__(self):
        self.state = {}

    async def eine(self, sql, params=None):
        return {"value": self.state["nutzer"]} if "nutzer" in self.state else None

    async def conn(self):
        import json
        store = self

        class C:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def execute(self, sql, params):
                store.state["nutzer"] = json.loads(params[0])

        return C()


def _kern(monkeypatch, env_land=None):
    from types import SimpleNamespace
    from qulsi_core.tools import Kern
    monkeypatch.delenv("QULSI_NUTZER", raising=False)
    if env_land:
        monkeypatch.setenv("QULSI_BUNDESLAND", env_land)
    else:
        monkeypatch.delenv("QULSI_BUNDESLAND", raising=False)
    k = Kern.__new__(Kern)
    k.store = _Store()
    k.nutzer_name, k.bundesland = None, None
    return k


def test_bundesland_je_box_mit_env_als_rueckfall(monkeypatch):
    import asyncio
    k = _kern(monkeypatch, env_land="NW")
    asyncio.run(k.nutzer_laden())
    assert k.bundesland == "NW"
    asyncio.run(k.nutzer_setzen("Kim"))
    assert asyncio.run(k.user_region_set("Bayern")).startswith("Gespeichert: Der Nutzer wohnt in Bayern")
    assert k.bundesland == "BY" and k.nutzer_name == "Kim"  # Name bleibt beim Ändern des Landes
    asyncio.run(k.nutzer_setzen("Alex"))
    assert k.store.state["nutzer"] == {"name": "Alex", "bundesland": "BY"}
    assert asyncio.run(k.holidays_list(ab="2026-11-01", anzahl=1))["bundesland"] == "Bayern"
    assert "kein deutsches Bundesland" in asyncio.run(k.user_region_set("Tirol")) and k.bundesland == "BY"
    asyncio.run(k.bundesland_setzen(None))
    assert k.bundesland == "NW"  # gelöscht: zurück auf QULSI_BUNDESLAND
