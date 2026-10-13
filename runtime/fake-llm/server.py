"""Test-Modell für Entwicklung ohne GPU.

Spricht die OpenAI-API wie llama-server, entscheidet aber mit festen Regeln, welches
Werkzeug es aufruft. Damit lässt sich der ganze Stack (LiteLLM, Core, Hub,
Connectoren, Datenbank) im Container testen, ohne Bonsai zu laden.
Nicht für den echten Betrieb.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid

from fastapi import FastAPI, Request

app = FastAPI(title="fake-llm")
ANFRAGEN: list[dict] = []

REGELN = [
    # Anhang aus der App: Dokument erst lesen (wie es der Systemprompt verlangt)
    (re.compile(r'\[Anhang: Dokument [^\]]*datei="(?P<d>[^"]+)"'), "files_read", lambda m: {"datei": m["d"]}),
    (re.compile(r"\bwo (?:ist|sind|liegt|liegen) (?:mein(?:e|en)? |die |der |das )?(?P<x>[\wäöüß]+)", re.I),
     "objects_where", lambda m: {"objekt": m["x"]}),
    (re.compile(r"schau (?:mal )?(?:in (?:der|die|den) )?(?P<k>kueche|küche|flur|wohnzimmer)", re.I),
     "camera_capture", lambda m: {"kamera": m["k"].lower().replace("ü", "ue")}),
    (re.compile(r"(?:mach|schalte?) (?:das |die )?(?:licht|lampe) im (?P<r>\w+) (?P<a>an|aus)", re.I),
     "home_set_state", lambda m: {"entity_id": f"light.{m['r'].lower()}", "aktion": "turn_on" if m["a"] == "an" else "turn_off"}),
    (re.compile(r"(?P<f>ich bin [\d,]+ (?:jahre alt|meter groß)),? merk dir das", re.I), "memory_remember",
     lambda m: {"text": m["f"], "art": "semantisch"}),
    (re.compile(r"^ich bin (?P<n>[a-zäöüß]+)\.?$", re.I), "user_name_set", lambda m: {"name": m["n"]}),
    (re.compile(r"^vergiss das mit dem testwort", re.I), "memory_forget", lambda m: {"id": 1}),
    (re.compile(r"^ja,? mach das\.?$", re.I), "memory_forget", lambda m: {"id": 1}),
    (re.compile(r"wor(?:ü|ue)ber haben wir .*gesprochen|altes gespr(?:ä|ae)ch", re.I), "conversations_search",
     lambda m: {}),
    (re.compile(r"^ich wohne in (?P<l>[a-zäöüß-]+)\.?$", re.I), "user_region_set", lambda m: {"bundesland": m["l"]}),
    (re.compile(r"heute (?P<z>\d{1,2}:\d\d) uhr (?P<t>\w+)", re.I), "calendar_create",
     lambda m: {"titel": m["t"], "start": f"{time.strftime('%Y-%m-%d')}T{int(m['z'].split(':')[0]):02d}:{m['z'].split(':')[1]}"}),
    (re.compile(r"merk dir,? (?:dass )?(?P<f>.+)", re.I), "memory_remember", lambda m: {"text": m["f"], "art": "semantisch"}),
    (re.compile(r"was wei(?:ß|ss)t du über (?P<q>.+?)\??$", re.I), "memory_search", lambda m: {"suche": m["q"]}),
    (re.compile(r"(?P<n>[A-ZÄÖÜ]\w+) ist mein(?:e)? (?P<b>\w+),? (?:seine|ihre) (?:mail|e-mail)(?:adresse)? ist (?P<m>\S+?)\.?$", re.I),
     "persons_save", lambda m: {"name": m["n"], "beziehung": m["b"], "mail": m["m"]}),
    # Mehrschritt: persons_search -> email_draft -> email_send (siehe FOLGE)
    (re.compile(r"(?:schick|sende|schreib) (?P<n>[A-ZÄÖÜ]\w+) eine (?:mail|nachricht)(?:,? (?:dass|:) ?(?P<t>.+))?", re.I),
     "persons_search", lambda m: {"name": m["n"]}),
    (re.compile(r"(?:lies|zeig) (?:mir )?(?:meine )?mails", re.I), "email_search", lambda m: {"suche": "", "tage": 3}),
    (re.compile(r"(?:öffne|entriegle) (?:die )?(?:haus)?tür", re.I), "door_unlock", lambda m: {"entity_id": "lock.haustuer"}),
    (re.compile(r"suche im web nach (?P<q>.+)", re.I), "browser_search", lambda m: {"suche": m["q"]}),
    (re.compile(r"was habe ich (?:heute|morgen)?.*vor", re.I), "calendar_search", lambda m: {}),
    (re.compile(r"feiertag", re.I), "holidays_list", lambda m: {"anzahl": 2}),
    (re.compile(r"(?:neue aufgabe|erinnere mich an|aufgabe):? (?P<t>.+)", re.I), "tasks_create", lambda m: {"titel": m["t"]}),
    (re.compile(r"welche aufgaben", re.I), "tasks_list", lambda m: {}),
    (re.compile(r"was steht in meinen dateien über (?P<q>.+?)\??$", re.I), "files_search", lambda m: {"suche": m["q"]}),
    (re.compile(r"was ist heute passiert", re.I), "memory_timeline", lambda m: {}),
    (re.compile(r"^(?:finde|was war)\b.*", re.I), "memory_find", lambda m: {"frage": m[0]}),
]

MAIL_RE = next(r for r, t, _ in REGELN if t == "persons_search")


def folge(name: str, inhalt: str, msgs: list[dict]) -> tuple[str, dict] | None:
    """Nächster Werkzeugaufruf eines Mehrschritt-Ablaufs, sonst None."""
    frage = next((m.get("content") or "" for m in reversed(msgs) if m.get("role") == "user"), "")
    m = MAIL_RE.search(frage if isinstance(frage, str) else "")
    if not m:
        return None
    try:
        daten = json.loads(inhalt)
    except json.JSONDecodeError:
        return None
    if name == "persons_search" and isinstance(daten, dict) and (daten.get("kontakt") or {}).get("mail"):
        return "email_draft", {"an": daten["kontakt"]["mail"], "betreff": "Kurze Nachricht", "text": m["t"] or "Hallo!"}
    if name == "email_draft" and isinstance(daten, dict) and daten.get("entwurf_id"):
        return "email_send", {"entwurf_id": daten["entwurf_id"]}
    return None


def _auspacken(inhalt: str) -> str:
    """Fremde Inhalte kommen im Schutz-Umschlag (<<<FREMDER_INHALT ...>>>): für die Testantwort nur der Inhalt."""
    if not inhalt.startswith("<<<FREMDER_INHALT"):
        return inhalt
    zeilen = [z for z in inhalt.splitlines() if not z.startswith(("<<<FREMDER_INHALT", "<<<ENDE_FREMDER_INHALT"))
              and not z.startswith("Das Folgende sind Daten aus einer fremden Quelle")]
    return "\n".join(zeilen).strip()


def antwort_aus_werkzeug(name: str, inhalt: str) -> str:
    inhalt = _auspacken(inhalt)
    try:
        daten = json.loads(inhalt)
    except json.JSONDecodeError:
        return inhalt[:300]
    if name == "objects_where" and isinstance(daten, dict):
        k = daten.get("kamera") or daten.get("bluetooth")
        if not daten.get("gefunden") or not k:
            return f"Ich habe {daten.get('objekt')} noch nirgends gesehen."
        neben = f", neben {', '.join(k['neben'])}" if k.get("neben") else ""
        return f"{daten['objekt']}: {k['ort']} ({k['raum']}){neben}, zuletzt gesehen {k['zuletzt_gesehen']}."
    if name == "holidays_list" and isinstance(daten, dict):
        f = daten.get("feiertage") or []
        return "Dazu habe ich nichts gefunden." if not f else \
            "Nächster Feiertag: " + "; ".join(f"{x['name']} am {x['datum']} ({x['gilt_in']})" for x in f)
    if name == "calendar_search" and isinstance(daten, list):
        if not daten:
            return "Heute stehen keine Termine an."
        return "Heute: " + "; ".join(f"{t['von']} {t['titel']}" for t in daten)
    return json.dumps(daten, ensure_ascii=False)[:300]


def nachricht(content: str | None = None, tool: tuple[str, dict] | None = None) -> dict:
    msg: dict = {"role": "assistant", "content": content}
    if tool:
        msg["tool_calls"] = [{
            "id": f"call_{uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {"name": tool[0], "arguments": json.dumps(tool[1], ensure_ascii=False)},
        }]
    return msg


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "bonsai", "object": "model"}, {"id": "qulsi-klein", "object": "model"}, {"id": "embed", "object": "model"}]}


@app.get("/debug/anfragen")
async def debug():
    return ANFRAGEN[-20:]


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    ANFRAGEN.append(body)
    msgs = body.get("messages", [])
    tools = {t["function"]["name"] for t in body.get("tools") or []}
    letzte = msgs[-1] if msgs else {}

    if letzte.get("role") == "tool":
        # Name des Werkzeugs aus dem vorherigen Assistant-Aufruf holen
        name = next((c["function"]["name"] for m in reversed(msgs) if m.get("tool_calls") for c in m["tool_calls"]), "")
        weiter = folge(name, letzte.get("content") or "", msgs)
        if weiter and weiter[0] in tools:
            msg = nachricht(None, weiter)
        else:
            msg = nachricht(antwort_aus_werkzeug(name, letzte.get("content") or ""))
    else:
        text = letzte.get("content") or ""
        if isinstance(text, list):
            text = " ".join(p.get("text", "") for p in text if isinstance(p, dict))
        msg = nachricht("Dazu habe ich kein passendes Werkzeug.")
        if re.search(r"wie hei(?:ß|ss)e ich", text, re.I):  # Name nur aus dem Systemprompt, wie es Bonsai soll
            system = next((m.get("content") or "" for m in msgs if m.get("role") == "system"), "")
            n = re.search(r"Dein Nutzer heißt (\w+)", system)
            msg = nachricht(f"Du heißt {n[1]}." if n else "Das weiß ich nicht. Wie heißt du?")
        if text.lower().startswith(("ist das", "zeigt das")):
            msg = nachricht("Ja, das sieht nach dem gesuchten Gegenstand aus.")
        for regex, tool, args in REGELN:
            m = regex.search(text)
            if m and tool in tools:
                msg = nachricht(None, (tool, args(m)))
                break
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", "bonsai"),
        "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _embedding(text: str, dim: int = 1024) -> list[float]:
    """Bag-of-words mit Hashing: gleiche Wörter ergeben ähnliche Vektoren."""
    v = [0.0] * dim
    for wort in re.findall(r"[\wäöüß]+", text.lower()):
        h = int(hashlib.md5(wort.encode()).hexdigest(), 16)
        v[h % dim] += 1.0 if (h >> 20) & 1 else -1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    body = await request.json()
    inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
    return {
        "object": "list",
        "model": body.get("model", "embed"),
        "data": [{"object": "embedding", "index": i, "embedding": _embedding(t)} for i, t in enumerate(inputs)],
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }
