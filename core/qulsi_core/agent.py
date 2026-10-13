"""Agent Runtime: Model Router fragen, Modell fragen, Werkzeuge über den Hub ausführen, wiederholen."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from . import schutz
from .hub import FREIGABE_HINWEIS, Hub
from .policy import Kontext
from .router import WOCHENTAGE, Route, Router

log = logging.getLogger(__name__)

THINK_RE = re.compile(r"<think>.*?</think>", re.S)
# Werkzeugaufrufe, die als Text im Inhalt landen (llama-server hat sie nicht erkannt):
# Qwen3-Coder-Stil <tool_call><function=name><parameter=x>wert</parameter></function></tool_call>
# und Hermes-Stil <tool_call>{"name": ..., "arguments": {...}}</tool_call>
TOOL_CALL_RE = re.compile(r"<tool_call>(.*?)(?:</tool_call>|$)", re.S)
FUNKTION_RE = re.compile(r"<function=([\w.\-]+)>(.*?)(?:</function>|$)", re.S)
PARAMETER_RE = re.compile(r"<parameter=([\w.\-]+)>\s*(.*?)\s*</parameter>", re.S)
# Behauptet die Antwort eine Tat ("habe ... gefunden", "ist eingetragen", "ID 38"), ohne dass in diesem Zug
# ein Werkzeug lief, ist sie erfunden (Fall: "Heut 9:30 Arzttermin" -> "Termin gefunden (ID 38)").
# Verneint ("habe noch nichts gespeichert", "habe ich nicht nachgesehen") ist keine Behauptung.
BEHAUPTUNG_RE = re.compile(
    r"\b(?:habe|hab)\b(?:(?!\b(?:nicht|nichts|kein\w*|nie)\b)[^.!?\n]){0,80}?\b(?:gefunden|gespeichert|angelegt|eingetragen|notiert|gemerkt|erstellt|erledigt|"
    r"gelöscht|gesendet|geschickt|abgehakt|nachgesehen|geprüft|umbenannt|vermerkt)\b"
    r"|\b(?:ist|sind|wurde|wurden)\s+(?:jetzt\s+|nun\s+)?(?:gespeichert|eingetragen|angelegt|notiert|erledigt|gelöscht|gesendet|vermerkt)\b"
    r"|\bID\s*\d+", re.I)
NACHHAKEN = ("Prüfung von QuLSI: In dieser Antwort hast du kein Werkzeug benutzt, schreibst aber, du hättest etwas "
             "gefunden, gespeichert oder eingetragen. Das stimmt so nicht. Benutze jetzt das passende Werkzeug "
             "(zum Beispiel calendar_create für einen neuen Termin, calendar_search zum Nachsehen) oder antworte "
             "ehrlich, ohne etwas zu behaupten.")
ERFUNDEN = ("Das habe ich noch nicht nachgesehen und auch nichts gespeichert. Sag mir bitte genauer, was ich tun soll, "
            "zum Beispiel „Trag den Termin ein“ oder „Welche Termine habe ich heute?“.")
# "Wie heiße ich?" beantwortet QuLSI selbst aus den Einstellungen: das Modell soll hier nichts behaupten
NAME_FRAGE_RE = re.compile(r"^\W*(?:(?:und\s+)?wie\s+hei(?:ß|ss)e\s+ich|wie\s+ist\s+mein\s+name|"
                           r"(?:weißt|weisst|kennst)\s+du,?\s+(?:wie\s+ich\s+hei(?:ß|ss)e|meinen\s+namen))\W*$", re.I)
NAME_UNBEKANNT = ("Deinen Namen kenne ich noch nicht. Wie heißt du? Sag zum Beispiel „Ich heiße Kim“, "
                  "dann merke ich ihn mir.")
# Der Produktname steht immer in Großbuchstaben, auch wenn der Nutzer oder das Modell ihn anders schreibt
PRODUKT_RE = re.compile(r"(?<![\w./@:-])qulsi(?!\w|[_./@:]\w)", re.I)
WIEDERHOLUNGEN = 3  # dasselbe Werkzeug öfter als so oft in einer Antwort: Schleife, abbrechen


class LLM:
    """OpenAI-kompatibler Client für LiteLLM."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def chat(self, model: str, messages: list[dict], tools: list[dict] | None) -> dict:
        body: dict = {"model": model, "messages": messages}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        async with httpx.AsyncClient(timeout=300, trust_env=False) as client:
            r = await client.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            r.raise_for_status()
            wahl = r.json()["choices"][0]
            msg = wahl["message"]
            if wahl.get("finish_reason") == "length":  # Kontext oder Tokenlimit voll: Text endet mitten im Satz
                log.warning("Antwort von %s abgeschnitten (finish_reason=length, %d Nachrichten)", model, len(messages))
                msg["_abgeschnitten"] = True
            return msg


@dataclass
class Antwort:
    text: str
    route: Route
    aufrufe: list[dict] = field(default_factory=list)
    offline: bool = False


def ort_satz() -> str:
    """Wo QuLSI läuft, damit es nicht „bei dir zu Hause“ behauptet, wenn es auf RunPod läuft."""
    if os.environ.get("QULSI_LAUFZEIT") == "runpod":
        return ("Du läufst auf einem gemieteten Server deines Nutzers in der Cloud (RunPod), nicht bei ihm zu Hause. "
                "Die Daten liegen auf diesem Server, nicht bei einem KI-Anbieter.")
    return "Du läufst lokal auf der QuLSI-Box deines Nutzers bei ihm zu Hause."


def produktname(text: str) -> str:
    """„QuLSI“ in der Antwort wird zu „QuLSI“ (Adressen wie qulsi.local bleiben)."""
    return PRODUKT_RE.sub("QuLSI", text)


def nutzer_satz(name: str | None) -> str:
    """Was QuLSI über den Namen weiß. Kein fest eingebauter Name, nie raten."""
    if name:
        return f"Dein Nutzer heißt {name} (eingetragen in der App oder von ihm selbst genannt)."
    return ("Wie dein Nutzer heißt, weißt du nicht: sprich ihn ohne Namen an und erfinde keinen. Fragt er danach, "
            "sag ehrlich, dass du es nicht weißt, und frag nach. Nennt er seinen Namen, speichere ihn mit user_name_set.")


class Agent:
    def __init__(self, llm: LLM, hub: Hub, router: Router, prompt_path: str, tz: str, max_schritte: int):
        self.llm = llm
        self.hub = hub
        self.router = router
        self.prompt_vorlage = Path(prompt_path).read_text(encoding="utf-8")
        self.tz = ZoneInfo(tz)
        self.max_schritte = max_schritte

    def system_prompt(self, ctx: Kontext, offline: bool) -> str:
        jetzt = datetime.now(self.tz)
        kanal = "Sprache (kurz antworten, ein bis zwei Sätze, keine Aufzählungen)" if ctx.kanal == "stimme" else "Chat"
        if offline:
            kanal += ". Offline-Modus: kein Internet, keine Websuche, keine Cloud-Mail"
        return self.prompt_vorlage.format(jetzt=f"{WOCHENTAGE[jetzt.weekday()]}, {jetzt:%d.%m.%Y %H:%M}", kanal=kanal, ort=ort_satz(),
                                          nutzer=nutzer_satz(getattr(getattr(self.hub, "kern", None), "nutzer_name", None)))

    async def antworten(self, verlauf: list[dict], ctx: Kontext, erzwingen: str | None = None) -> Antwort:
        letzte = next((m for m in reversed(verlauf) if m.get("role") == "user"), {"content": ""})
        text, hat_bild = _text_und_bild(letzte.get("content"))
        route = self.router.waehlen(text, ctx.kanal, hat_bild, erzwingen)
        if route.art == "system":
            return Antwort(route.antwort or "", route)
        if not hat_bild and NAME_FRAGE_RE.match(text.strip()):
            name = getattr(getattr(self.hub, "kern", None), "nutzer_name", None)
            return Antwort(f"Du heißt {name}." if name else NAME_UNBEKANNT,
                           Route("system", grund="Name aus den Einstellungen"))

        offline = await self.hub.offline()
        try:
            text, aufrufe = await self._schleife(route, verlauf, ctx, offline)
            if not text and route.ersatz:
                raise ValueError("leere Antwort")
            # Das kleine Modell bekommt nur Befehle, die ein Werkzeug brauchen ("Merk dir ...").
            # Antwortet es nur mit Text, ist nichts passiert: dann übernimmt das Standardmodell.
            if route.art == "klein" and route.werkzeuge and not aufrufe and route.ersatz:
                raise ValueError("kein Werkzeug aufgerufen")
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            if not route.ersatz:
                raise
            log.info("Modell %s gescheitert (%s), übernimmt %s", route.modell, exc, route.ersatz)
            route = Route("standard", route.ersatz, grund=f"Ersatz für {route.modell}")
            text, aufrufe = await self._schleife(route, verlauf, ctx, offline)
        return Antwort(produktname(text), route, aufrufe, offline)

    async def _schleife(self, route: Route, verlauf: list[dict], ctx: Kontext, offline: bool) -> tuple[str, list[dict]]:
        messages = [{"role": "system", "content": self.system_prompt(ctx, offline)}]
        for m in verlauf:
            if m.get("role") not in ("user", "assistant"):
                continue
            inhalt = m.get("content")
            if route.art != "vision":
                inhalt = _text_und_bild(inhalt)[0]
            messages.append({"role": m["role"], "content": inhalt})
        tools = self.hub.openai_tools(offline) if route.werkzeuge else None
        schutz.spur_von(ctx).nutzer_setzen(verlauf)  # Adressen, die der Nutzer selbst nennt, sind seine Wahl
        aufrufe: list[dict] = []
        assert route.modell

        gesehen: dict[str, str] = {}   # Aufruf (Name + Argumente) -> Ergebnis
        je_werkzeug: dict[str, int] = {}
        nachgehakt = False
        for _ in range(self.max_schritte):
            msg = await self.llm.chat(route.modell, messages, tools)
            calls = msg.get("tool_calls") or []
            inhalt = msg.get("content") or ""
            if not calls and tools:
                calls = xml_aufrufe(inhalt)
                if calls:
                    log.info("Werkzeugaufruf als Text erkannt (%s)", ", ".join(c["function"]["name"] for c in calls))
                    inhalt = ohne_aufrufe(inhalt)
            if not calls:
                text = _fertig(inhalt, msg)
                if tools and not aufrufe and BEHAUPTUNG_RE.search(text):
                    if not nachgehakt:
                        log.info("Antwort behauptet eine Tat ohne Werkzeug, frage nach: %.80s", text)
                        nachgehakt = True
                        messages += [{"role": "assistant", "content": inhalt}, {"role": "user", "content": NACHHAKEN}]
                        continue
                    log.warning("Antwort behauptet weiter eine Tat ohne Werkzeug, verworfen: %.80s", text)
                    text = ERFUNDEN
                return _mit_freigabe_hinweis(text, aufrufe), aufrufe
            messages.append({"role": "assistant", "content": inhalt, "tool_calls": calls})
            schleife = False
            for call in calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                schluessel = name + json.dumps(args, sort_keys=True, ensure_ascii=False)
                je_werkzeug[name] = je_werkzeug.get(name, 0) + 1
                if schluessel in gesehen or je_werkzeug[name] > WIEDERHOLUNGEN:
                    # Schleifenschutz: nicht noch einmal ausführen, das Modell soll antworten
                    schleife = True
                    ergebnis = gesehen.get(schluessel) or "Dieses Werkzeug wurde schon mehrfach aufgerufen. Antworte jetzt mit dem, was du hast."
                else:
                    ergebnis = await self.hub.aufrufen(name, args, ctx)
                    gesehen[schluessel] = ergebnis
                    aufrufe.append({"tool": name, "arguments": args, "ergebnis": ergebnis})
                messages.append({"role": "tool", "tool_call_id": call.get("id", name),
                                 "content": schutz.verpacken_wenn_fremd(ergebnis, ctx)})
            if schleife:
                log.info("Schleife bei %s erkannt, Antwort ohne weitere Werkzeuge", route.modell)
                break

        # Schrittlimit oder Schleife: ohne Werkzeuge um die Antwort bitten. Manche Modelle schreiben dann
        # trotzdem einen Werkzeugaufruf als Text; der wird entfernt.
        messages.append({"role": "user", "content": "Beantworte jetzt meine Frage mit den Ergebnissen oben, ohne weitere Werkzeuge."})
        msg = await self.llm.chat(route.modell, messages, None)
        text = _fertig(msg.get("content") or "", msg)
        if not text and aufrufe:
            text = "Ich habe nachgesehen, konnte aber keine Antwort formulieren. Gefunden habe ich:\n" + aufrufe[-1]["ergebnis"][:800]
        return _mit_freigabe_hinweis(text, aufrufe), aufrufe


def _mit_freigabe_hinweis(text: str, aufrufe: list[dict]) -> str:
    """Wartet eine Aktion auf Freigabe, steht der Hinweis sicher in der Antwort, auch wenn das Modell ihn weglässt."""
    if any(FREIGABE_HINWEIS in str(a.get("ergebnis", "")) for a in aufrufe) and FREIGABE_HINWEIS not in text:
        return f"{text.rstrip()}\n\n{FREIGABE_HINWEIS}".strip()
    return text


def _text_und_bild(content) -> tuple[str, bool]:
    """Inhalte kommen als Text oder als Liste von Teilen (Text, Bild)."""
    if isinstance(content, list):
        text = "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
        bild = any(isinstance(p, dict) and p.get("type") == "image_url" for p in content)
        return text, bild
    return content or "", False


ABGESCHNITTEN = ("(Meine Antwort wurde hier abgeschnitten, das Gespräch ist zu lang geworden. "
                 "Am besten fängst du mit „Neues Gespräch“ neu an.)")


def _fertig(inhalt: str, msg: dict) -> str:
    """Antworttext ohne Denkblock und ohne Werkzeugaufrufe, die das Modell als Text geschrieben hat.

    Ein solcher Aufruf mitten im Text (oft in der letzten Runde ohne Werkzeuge) würde sonst den Rest
    abschneiden, ohne dass es jemand merkt: das wird protokolliert und der Satz sauber beendet.
    """
    text = _bereinigen(ohne_aufrufe(inhalt))
    aufruf_im_text = "<tool_call>" in inhalt or "<function=" in inhalt
    if aufruf_im_text:
        log.info("Werkzeugaufruf im Antworttext entfernt (%d Zeichen davor)", len(text))
    if not text:
        return text
    if msg.get("_abgeschnitten"):
        return text.rstrip() + " …\n\n" + ABGESCHNITTEN
    if aufruf_im_text and not text.endswith((".", "!", "?", ")", "\"", "“", "*")):
        return text + " …\n\n(Den Rest konnte ich nicht ausführen, dafür fehlt mir hier ein passendes Werkzeug.)"
    return text


def _bereinigen(text: str) -> str:
    return THINK_RE.sub("", text).strip()


def _wert(roh: str):
    try:
        return json.loads(roh)
    except (json.JSONDecodeError, ValueError):
        return roh


def xml_aufrufe(text: str) -> list[dict]:
    """Werkzeugaufrufe aus dem Text holen, wenn der Server sie nicht als tool_calls geliefert hat."""
    calls = []
    bloecke = TOOL_CALL_RE.findall(text) or ([text] if "<function=" in text else [])
    for block in bloecke:
        block = block.strip()
        if block.startswith("{"):
            try:
                d = json.loads(block)
                calls.append((d["name"], d.get("arguments") or d.get("parameters") or {}))
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
            continue
        for name, rumpf in FUNKTION_RE.findall(block):
            calls.append((name, {k: _wert(v) for k, v in PARAMETER_RE.findall(rumpf)}))
    return [{"id": f"text-{i}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
            for i, (name, args) in enumerate(calls)]


def ohne_aufrufe(text: str) -> str:
    text = TOOL_CALL_RE.sub("", text)
    return FUNKTION_RE.sub("", text).strip()
