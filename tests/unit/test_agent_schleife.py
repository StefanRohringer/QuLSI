"""Agent-Schleife mit einem Ersatz-LLM: Werkzeugaufrufe als Text, Schleifenschutz, kleines Modell ohne Werkzeug."""

import asyncio
import json
from pathlib import Path

from qulsi_core.agent import Agent, ohne_aufrufe, xml_aufrufe
from qulsi_core.policy import Kontext
from qulsi_core.router import Route

ROOT = Path(__file__).resolve().parents[2]
XML = ("<tool_call>\n<function=memory_search>\n<parameter=suche>\nKaffee\n</parameter>\n"
       "<parameter=limit>\n5\n</parameter>\n</function>\n</tool_call>")


class Hub:
    def __init__(self):
        self.aufgerufen = []

    def openai_tools(self, offline):
        return [{"type": "function", "function": {"name": "memory_search", "parameters": {}}}]

    async def offline(self):
        return False

    async def aufrufen(self, name, args, ctx):
        self.aufgerufen.append((name, args))
        return "Anna trinkt Kaffee schwarz."


class LLM:
    def __init__(self, antworten):
        self.antworten = list(antworten)
        self.anfragen = []

    async def chat(self, model, messages, tools):
        self.anfragen.append((model, tools is not None))
        return self.antworten.pop(0)


class Router:
    def __init__(self, route):
        self.route = route

    def waehlen(self, *a):
        return self.route


def agent(llm, hub, route):
    return Agent(llm, hub, Router(route), str(ROOT / "config/agent_prompt.md"), "Europe/Berlin", 6)


def frage(a):
    return asyncio.run(a.antworten([{"role": "user", "content": "Was weißt du über Kaffee?"}], Kontext(kanal="chat")))


def test_xml_aufruf_wird_erkannt():
    calls = xml_aufrufe("Ich schaue nach.\n" + XML)
    assert calls[0]["function"]["name"] == "memory_search"
    assert json.loads(calls[0]["function"]["arguments"]) == {"suche": "Kaffee", "limit": 5}
    assert ohne_aufrufe("Ich schaue nach.\n" + XML) == "Ich schaue nach."
    hermes = xml_aufrufe('<tool_call>{"name": "tasks_list", "arguments": {"offen": true}}</tool_call>')
    assert hermes[0]["function"]["name"] == "tasks_list"


def test_xml_aufruf_wird_ausgefuehrt_und_beantwortet():
    hub = Hub()
    llm = LLM([{"content": XML}, {"content": "Du trinkst Kaffee schwarz."}])
    r = frage(agent(llm, hub, Route("standard", "bonsai-fast")))
    assert hub.aufgerufen == [("memory_search", {"suche": "Kaffee", "limit": 5})]
    assert r.text == "Du trinkst Kaffee schwarz."


def test_schleife_wird_abgebrochen_und_kein_xml_in_der_antwort():
    hub = Hub()
    # Das Modell ruft immer wieder dasselbe auf und schreibt am Ende noch einmal XML statt einer Antwort
    llm = LLM([{"content": XML}, {"content": XML}, {"content": XML}])
    r = frage(agent(llm, hub, Route("standard", "bonsai-fast")))
    assert len(hub.aufgerufen) == 1          # derselbe Aufruf wird nicht wiederholt
    assert "<tool_call>" not in r.text and "<function=" not in r.text
    assert "Kaffee schwarz" in r.text        # Ersatzantwort aus dem Ergebnis
    assert llm.anfragen[-1] == ("bonsai-fast", False)


def test_kleines_modell_ohne_werkzeug_uebergibt_an_standard():
    hub = Hub()
    llm = LLM([{"content": "Okay, gemerkt!"},
               {"tool_calls": [{"id": "1", "function": {"name": "memory_search", "arguments": "{}"}}]},
               {"content": "Gespeichert."}])
    r = frage(agent(llm, hub, Route("klein", "qulsi-klein", ersatz="bonsai-fast")))
    assert [m for m, _ in llm.anfragen] == ["qulsi-klein", "bonsai-fast", "bonsai-fast"]
    assert r.route.modell == "bonsai-fast" and hub.aufgerufen and r.text == "Gespeichert."


def test_erfundene_tat_ohne_werkzeug_wird_nachgefragt():
    # Fall: "Heut 9:30 Arzttermin" -> "Ich habe einen Termin ... gefunden (ID 38)" ohne Werkzeugaufruf
    from qulsi_core.agent import ERFUNDEN, NACHHAKEN
    hub = Hub()
    llm = LLM([{"content": "Ich habe einen Termin um 09:30 gefunden (ID 38)."},
               {"content": "", "tool_calls": [{"id": "1", "function": {"name": "memory_search", "arguments": "{}"}}]},
               {"content": "Laut Gedächtnis trinkst du Kaffee schwarz."}])
    a = frage(agent(llm, hub, Route("standard", "bonsai-fast")))
    assert a.text == "Laut Gedächtnis trinkst du Kaffee schwarz." and hub.aufgerufen
    llm = LLM([{"content": "Termin ist eingetragen."}, {"content": "Doch, ich habe ihn gespeichert."}])
    a = frage(agent(llm, Hub(), Route("standard", "bonsai-fast")))
    assert a.text == ERFUNDEN and NACHHAKEN
    llm = LLM([{"content": "Hallo! Wie kann ich helfen?"}])
    assert frage(agent(llm, Hub(), Route("standard", "bonsai-fast"))).text == "Hallo! Wie kann ich helfen?"


def test_freigabe_hinweis_steht_immer_in_der_antwort():
    from qulsi_core.agent import _mit_freigabe_hinweis
    from qulsi_core.hub import FREIGABE_HINWEIS
    wartet = [{"tool": "mail_send", "ergebnis": f"Noch nicht ausgeführt. Sag dem Nutzer wörtlich: „{FREIGABE_HINWEIS}“"}]
    assert _mit_freigabe_hinweis("Der Entwurf ist fertig.", wartet).endswith(FREIGABE_HINWEIS)
    assert _mit_freigabe_hinweis(f"Gern. {FREIGABE_HINWEIS}", wartet).count(FREIGABE_HINWEIS) == 1
    assert _mit_freigabe_hinweis("Erledigt.", [{"tool": "tasks_list", "ergebnis": "keine"}]) == "Erledigt."
