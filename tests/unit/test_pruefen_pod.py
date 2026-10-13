"""Fälle aus `qulsi.sh pruefen` auf dem Pod (2026.10.08-0300): „Name unbekannt“ und „System erklären“."""

import asyncio
from pathlib import Path

from qulsi_core.abnahme import PROMPT_SAETZE
from qulsi_core.agent import (BEHAUPTUNG_RE, ERFUNDEN, NAME_FRAGE_RE, NAME_UNBEKANNT, Agent, ort_satz, produktname)
from qulsi_core.policy import Kontext
from qulsi_core.router import Router

ROOT = Path(__file__).resolve().parents[2]


def test_verneinung_ist_keine_behauptung():
    assert not BEHAUPTUNG_RE.search(ERFUNDEN)
    assert not BEHAUPTUNG_RE.search("Ich habe deinen Namen noch nicht gespeichert.")
    assert not BEHAUPTUNG_RE.search("Dazu habe ich nichts gefunden.")
    assert BEHAUPTUNG_RE.search("Ich habe den Termin eingetragen.")
    assert BEHAUPTUNG_RE.search("Ich habe nachgesehen: nichts.")


def test_produktname_gross():
    assert produktname("QuLSI ist dein Assistent, die QuLSI-App auch.") == "QuLSI ist dein Assistent, die QuLSI-App auch."
    assert produktname("http://qulsi.local und qulsi_core") == "http://qulsi.local und qulsi_core"


def test_ort_je_laufzeit(monkeypatch):
    monkeypatch.setenv("QULSI_LAUFZEIT", "runpod")
    assert "RunPod" in ort_satz() and "nicht bei ihm zu Hause" in ort_satz()
    monkeypatch.delenv("QULSI_LAUFZEIT")
    assert "zu Hause" in ort_satz()


def test_prompt_ohne_pruefsaetze_zum_nachplappern():
    prompt = (ROOT / "config" / "agent_prompt.md").read_text(encoding="utf-8")
    assert "{ort}" in prompt
    assert "Daten, keine Anweisungen" not in prompt  # wurde auf dem Pod wörtlich nachgeplappert
    assert all(s in prompt for s in ("liest du nur", "Schreib deinen Namen"))  # pruefen sucht danach
    assert "liest du nur" in PROMPT_SAETZE


class _Kern:
    nutzer_name = None


class _Hub:
    kern = _Kern()

    async def offline(self):
        raise AssertionError("das Modell darf nicht gefragt werden")


class _LLM:
    async def chat(self, *a, **k):
        raise AssertionError("das Modell darf nicht gefragt werden")


def _agent():
    return Agent(_LLM(), _Hub(), Router(), str(ROOT / "config/agent_prompt.md"), "Europe/Berlin", 4)


def test_name_frage_ohne_modell():
    assert NAME_FRAGE_RE.match("Wie heiße ich?") and NAME_FRAGE_RE.match("Weißt du, wie ich heiße?")
    assert not NAME_FRAGE_RE.match("Wie heiße ich in der Firma?")
    a = _agent()
    ctx = Kontext(kanal="app")
    r = asyncio.run(a.antworten([{"role": "user", "content": "Wie heiße ich?"}], ctx))
    assert r.text == NAME_UNBEKANNT and "kenne ich noch nicht" in r.text
    _Kern.nutzer_name = "Kim"
    try:
        r = asyncio.run(a.antworten([{"role": "user", "content": "wie heisse ich"}], ctx))
        assert r.text == "Du heißt Kim."
    finally:
        _Kern.nutzer_name = None
