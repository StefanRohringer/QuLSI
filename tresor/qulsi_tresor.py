"""Tresor: Passwörter, API-Schlüssel und OAuth-Tokens der Connectoren, verschlüsselt mit age.

Die Geheimnisse liegen nicht in der .env und nicht im Connector, sondern nur hier:
- Ein Connector bekommt nur die Geheimnisse, die in seinem Manifest unter "secrets" stehen.
- OAuth (Google, Microsoft): Das langlebige Refresh-Token und das Client-Secret verlassen den Tresor
  nie. Der Connector bekommt nur kurzlebige Zugriffstokens (beim Anbieter meist 1 Stunde gültig,
  mit den Berechtigungen, die bei der Anmeldung erteilt wurden).
- Wer fragt, erkennt der Tresor wie der Egress-Proxy am Containernamen (Docker-DNS).
- Jeder Zugriff steht im Protokoll (DATA_DIR/zugriffe.jsonl).

Verschlüsselt ist die Datei DATA_DIR/tresor.age mit dem Schlüssel aus TRESOR_SCHLUESSEL
(Standard DATA_DIR/schluessel.txt, wird beim ersten Start erzeugt). Das schützt Backups und
kopierte Festplatten, solange der Schlüssel nicht mit im Backup liegt (scripts/backup.sh lässt ihn weg).

In der QuLSI-App (Einstellungen > Konten): setzen und Konten verbinden über /verwaltung, nur der Core
darf das, und erst nach Freigabe (PIN oder Handy). Werte gehen nur hinein, nie heraus.

Bedienen (auf der Box):
    docker compose run --rm tresor setzen IMAP_PASSWORD        Wert eingeben (wird nicht angezeigt)
    docker compose run --rm tresor liste                       nur Namen, keine Werte
    docker compose run --rm tresor entfernen IMAP_PASSWORD
    docker compose run --rm -T tresor importieren < .env       übernimmt alle Geheimnisse aus den Manifesten
"""

from __future__ import annotations

import asyncio
import getpass
import ipaddress
import json
import logging
import os
import re
import socket
import sys
import time
from pathlib import Path

import httpx
import pyrage
import yaml
from fastapi import FastAPI, HTTPException, Request
from pyrage import x25519

log = logging.getLogger("tresor")
DATA = Path(os.environ.get("DATA_DIR", "/data"))
CONNECTORS = Path(os.environ.get("CONNECTORS_DIR", "/connectors"))
SCHLUESSEL = Path(os.environ.get("TRESOR_SCHLUESSEL", DATA / "schluessel.txt"))
DATEI = DATA / "tresor.age"


# ---------------------------------------------------------------- Speicher


class Speicher:
    def __init__(self, datei: Path = DATEI, schluessel: Path = SCHLUESSEL):
        self.datei = datei
        self.schluessel = schluessel
        self._lock = asyncio.Lock()

    def _identitaet(self) -> x25519.Identity:
        if not self.schluessel.exists():
            self.schluessel.parent.mkdir(parents=True, exist_ok=True)
            self.schluessel.write_text(str(x25519.Identity.generate()) + "\n")
            self.schluessel.chmod(0o600)
            log.warning("Neuer Tresor-Schlüssel: %s (nicht ins Backup legen, getrennt aufheben)", self.schluessel)
        return x25519.Identity.from_str(self.schluessel.read_text().strip())

    def lesen(self) -> dict:
        if not self.datei.exists():
            return {"geheimnisse": {}, "konten": {}}
        return json.loads(pyrage.decrypt(self.datei.read_bytes(), [self._identitaet()]))

    def schreiben(self, daten: dict) -> None:
        roh = pyrage.encrypt(json.dumps(daten).encode(), [self._identitaet().to_public()])
        tmp = self.datei.with_suffix(".tmp")
        tmp.write_bytes(roh)
        tmp.chmod(0o600)
        tmp.replace(self.datei)

    async def aendern(self, fn) -> dict:
        async with self._lock:
            daten = self.lesen()
            ergebnis = fn(daten)
            self.schreiben(daten)
            return ergebnis


# ---------------------------------------------------------------- Manifeste und Identität


def manifeste() -> dict[str, dict]:
    out = {}
    for pfad in sorted(CONNECTORS.glob("*/manifest.yaml")):
        raw = yaml.safe_load(pfad.read_text(encoding="utf-8")) or {}
        if raw.get("name"):
            raw.setdefault("dienst", f"connector-{raw['name']}")
            out[raw["name"]] = raw
    return out


def erlaubte_namen(m: dict) -> set[str]:
    """Geheimnisse, die ein Connector direkt lesen darf (nicht das Client-Secret seines Kontos)."""
    return set(m.get("secrets") or [])


async def wer(request: Request, ms: dict[str, dict]) -> str:
    ip = request.client.host if request.client else ""
    if ipaddress.ip_address(ip).is_loopback:
        name = request.headers.get("x-qulsi-connector", "")  # RunPod: kooperativ, nur lokal
        if name in ms:
            return name
        raise HTTPException(403, "Unbekannter Absender")
    try:
        host = (await asyncio.to_thread(socket.gethostbyaddr, ip))[0].split(".")[0].lower()
    except OSError:
        raise HTTPException(403, "Unbekannter Absender") from None
    for name, m in ms.items():
        if re.fullmatch(rf"(.+[-_])?{re.escape(m['dienst'].lower())}([-_]\d+)?", host):
            return name
    raise HTTPException(403, "Unbekannter Absender")


def protokoll(**eintrag) -> None:
    eintrag = {"zeit": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **eintrag}
    log.info("ZUGRIFF %s", json.dumps(eintrag, ensure_ascii=False))
    try:
        with (DATA / "zugriffe.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(eintrag, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- Dienst

app = FastAPI(title="QuLSI-Tresor")
speicher = Speicher()


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/geheimnis/{name}")
async def geheimnis(request: Request, name: str):
    ms = manifeste()
    connector = await wer(request, ms)
    if name not in erlaubte_namen(ms[connector]):
        protokoll(connector=connector, geheimnis=name, ergebnis="verweigert")
        raise HTTPException(403, f"{name} steht nicht im Manifest von {connector}")
    wert = speicher.lesen()["geheimnisse"].get(name)
    protokoll(connector=connector, geheimnis=name, ergebnis="ok" if wert is not None else "fehlt")
    if wert is None:
        raise HTTPException(404, f"{name} ist nicht im Tresor (docker compose run --rm tresor setzen {name})")
    return {"wert": wert}


def _konto(ms: dict, connector: str) -> dict:
    k = dict(ms[connector].get("konto") or {})
    k["token_url"] = os.path.expandvars(str(k.get("token_url") or ""))
    if not k.get("token_url"):
        raise HTTPException(403, f"{connector} hat im Manifest kein Konto")
    return k


async def _token_endpunkt(k: dict, daten: dict, felder: dict) -> dict:
    """Ruft den Token-Endpunkt des Anbieters auf, mit Client-Secret aus dem Tresor (über den Egress-Proxy)."""
    anfrage = dict(felder)
    if k.get("client_id"):
        anfrage["client_id"] = daten["geheimnisse"].get(k["client_id"]) or os.environ.get(k["client_id"]) or anfrage.get("client_id", "")
    if k.get("client_secret") and daten["geheimnisse"].get(k["client_secret"]):
        anfrage["client_secret"] = daten["geheimnisse"][k["client_secret"]]
    async with httpx.AsyncClient(timeout=30) as c:  # HTTPS_PROXY zeigt auf den Egress-Proxy
        r = await c.post(k["token_url"], data=anfrage)
    try:
        antwort = r.json()
    except ValueError:
        antwort = {"error": f"HTTP {r.status_code}", "error_description": r.text[:200]}
    if r.status_code >= 400 and "error" not in antwort:
        antwort["error"] = f"HTTP {r.status_code}"
    return antwort


def _merken(daten: dict, connector: str, t: dict) -> None:
    alt = daten["konten"].get(connector, {})
    daten["konten"][connector] = {
        "access_token": t["access_token"],
        "refresh_token": t.get("refresh_token") or alt.get("refresh_token"),
        "laeuft_ab": time.time() + int(t.get("expires_in", 3600)),
        "scope": t.get("scope") or alt.get("scope", ""),
        "seit": alt.get("seit"),
    }


@app.post("/konto/token")
async def konto_token(request: Request):
    """Anmeldung abschließen (Code oder Gerätecode einlösen). Das Refresh-Token bleibt im Tresor."""
    ms = manifeste()
    connector = await wer(request, ms)
    k = _konto(ms, connector)
    felder = {a: str(b) for a, b in (await request.json()).items()}
    t = await _token_endpunkt(k, speicher.lesen(), felder)
    if "access_token" not in t:
        protokoll(connector=connector, konto="anmelden", ergebnis=t.get("error", "fehler"))
        return t
    await speicher.aendern(lambda d: _merken(d, connector, t))
    protokoll(connector=connector, konto="anmelden", ergebnis="ok")
    return {"access_token": t["access_token"], "expires_in": t.get("expires_in", 3600),
            "scope": t.get("scope", ""), "refresh_token": "im-tresor" if t.get("refresh_token") else None}


@app.get("/token")
async def token(request: Request):
    """Kurzlebiges Zugriffstoken für das Konto des fragenden Connectors."""
    ms = manifeste()
    connector = await wer(request, ms)
    k = _konto(ms, connector)
    daten = speicher.lesen()
    konto = daten["konten"].get(connector)
    if not konto or not konto.get("refresh_token"):
        protokoll(connector=connector, konto="token", ergebnis="nicht_verbunden")
        raise HTTPException(404, f"{connector} ist nicht verbunden")
    erneuern = request.query_params.get("erneuern") == "1"
    if erneuern or konto["laeuft_ab"] - 60 < time.time():
        t = await _token_endpunkt(k, daten, {"grant_type": "refresh_token", "refresh_token": konto["refresh_token"]})
        if "access_token" not in t:
            protokoll(connector=connector, konto="token", ergebnis=t.get("error", "fehler"))
            raise HTTPException(401, f"Anmeldung abgelaufen ({t.get('error')}). Bitte neu anmelden.")
        await speicher.aendern(lambda d: _merken(d, connector, t))
        konto = speicher.lesen()["konten"][connector]
    protokoll(connector=connector, konto="token", ergebnis="ok")
    return {"access_token": konto["access_token"], "laeuft_ab": konto["laeuft_ab"]}


@app.get("/konto/status")
async def konto_status(request: Request):
    ms = manifeste()
    connector = await wer(request, ms)
    konto = speicher.lesen()["konten"].get(connector) or {}
    return {"verbunden": bool(konto.get("refresh_token")), "scope": konto.get("scope", "")}


@app.post("/konto/abmelden")
async def konto_abmelden(request: Request):
    ms = manifeste()
    connector = await wer(request, ms)
    await speicher.aendern(lambda d: d["konten"].pop(connector, None))
    protokoll(connector=connector, konto="abmelden", ergebnis="ok")
    return {"ok": True}


# ---------------------------------------------------------------- Verwaltung (nur der Core)
#
# Damit Konten und Passwörter in der QuLSI-App eingerichtet werden können, ohne Terminal. Der Core
# ruft diese Endpunkte erst auf, wenn die Änderung freigegeben ist (Freigabe-PIN in der App oder
# ntfy am Handy). Werte gehen nur hinein, nie heraus: kein Endpunkt liefert ein Passwort, ein Token
# oder den Gerätecode. Ausnahme sind die OAuth-Client-IDs, die ohnehin im Anmeldelink stehen.

CORE_DIENST = os.environ.get("TRESOR_CORE_DIENST", "core")
# RunPod (alles in einem Container): Absender kommen über 127.0.0.1 und nennen sich im Kopf X-QuLSI-Connector.
# Nur dort eingeschaltet (run.sh setzt TRESOR_LOKAL=1); auf der Box zählt allein der Containername.
LOKAL = os.environ.get("TRESOR_LOKAL") == "1"
MAX_WERT = 4096
geraetecodes: dict[str, dict] = {}  # Microsoft-Anmeldung läuft: connector -> device_code (nur im Speicher)


async def nur_core(request: Request) -> None:
    ip = request.client.host if request.client else ""
    try:
        if ipaddress.ip_address(ip).is_loopback:
            if LOKAL and request.headers.get("x-qulsi-connector") == CORE_DIENST:
                return
            raise HTTPException(403, "Verwaltung nur für den Core")
        host = (await asyncio.to_thread(socket.gethostbyaddr, ip))[0].split(".")[0].lower()
    except (OSError, ValueError):
        raise HTTPException(403, "Verwaltung nur für den Core") from None
    if not re.fullmatch(rf"(.+[-_])?{re.escape(CORE_DIENST)}([-_]\d+)?", host):
        raise HTTPException(403, "Verwaltung nur für den Core")


def client_id_namen(ms: dict[str, dict]) -> set[str]:
    return {m["konto"]["client_id"] for m in ms.values() if (m.get("konto") or {}).get("client_id")}


@app.get("/verwaltung/uebersicht")
async def verwaltung_uebersicht(request: Request):
    """Welche Geheimnisse gesetzt sind (nur ja/nein) und welche Konten verbunden sind."""
    await nur_core(request)
    ms = manifeste()
    daten = speicher.lesen()
    return {
        "geheimnisse": {n: n in daten["geheimnisse"] for n in sorted(bekannte_namen())},
        "konten": {c: {"verbunden": bool(k.get("refresh_token")), "scope": k.get("scope", ""), "seit": k.get("seit")}
                   for c, k in daten["konten"].items()},
        "client_ids": {n: daten["geheimnisse"].get(n) or os.environ.get(n, "") for n in sorted(client_id_namen(ms))},
    }


@app.post("/verwaltung/geheimnisse")
async def verwaltung_setzen(request: Request):
    """{"werte": {"NAS_PASSWORD": "..."}}: nur Namen aus den Manifesten."""
    await nur_core(request)
    werte = (await request.json()).get("werte")
    namen = bekannte_namen()
    if not isinstance(werte, dict) or not werte:
        raise HTTPException(422, "werte fehlt")
    for n, w in werte.items():
        if n not in namen:
            raise HTTPException(422, f"{n} steht in keinem Manifest")
        if not isinstance(w, str) or not w or len(w) > MAX_WERT or "\0" in w:
            raise HTTPException(422, f"Ungültiger Wert für {n}")
    await speicher.aendern(lambda d: d["geheimnisse"].update(werte))
    for n in werte:
        protokoll(verwaltung="core", geheimnis=n, ergebnis="gesetzt")
    return {"gesetzt": sorted(werte)}


@app.post("/verwaltung/geheimnisse/entfernen")
async def verwaltung_entfernen(request: Request):
    await nur_core(request)
    namen = (await request.json()).get("namen") or []
    if not isinstance(namen, list) or not all(isinstance(n, str) for n in namen):
        raise HTTPException(422, "namen muss eine Liste sein")
    await speicher.aendern(lambda d: [d["geheimnisse"].pop(n, None) for n in namen])
    for n in namen:
        protokoll(verwaltung="core", geheimnis=n, ergebnis="entfernt")
    return {"entfernt": sorted(namen)}


def _verwaltetes_konto(connector: str) -> dict:
    ms = manifeste()
    if connector not in ms:
        raise HTTPException(404, f"Unbekannter Connector {connector}")
    k = _konto(ms, connector)
    if k.get("geraetecode_url"):
        k["geraetecode_url"] = os.path.expandvars(str(k["geraetecode_url"]))
    return k


async def _einloesen(connector: str, k: dict, felder: dict) -> dict:
    """Token-Endpunkt aufrufen und das Ergebnis speichern. Zurück geht nur verbunden/Fehler, nie ein Token."""
    t = await _token_endpunkt(k, speicher.lesen(), felder)
    if "access_token" not in t:
        return {"error": str(t.get("error", "fehler")), "error_description": str(t.get("error_description", ""))[:300]}
    if not t.get("refresh_token"):
        protokoll(verwaltung="core", connector=connector, konto="anmelden", ergebnis="ohne_refresh_token")
        return {"error": "kein_refresh_token",
                "error_description": "Der Anbieter hat keinen dauerhaften Zugang erteilt. Bitte neu anmelden."}

    def merken(d: dict) -> None:
        d["konten"].pop(connector, None)  # neues Konto, altes Refresh-Token nicht weiterverwenden
        _merken(d, connector, t)
        d["konten"][connector]["seit"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    await speicher.aendern(merken)
    protokoll(verwaltung="core", connector=connector, konto="anmelden", ergebnis="ok")
    return {"verbunden": True, "scope": t.get("scope", "")}


@app.post("/verwaltung/konto/{connector}/einloesen")
async def verwaltung_einloesen(connector: str, request: Request):
    """Google: Code aus dem Anmeldelink einlösen (PKCE). Der Core schickt code, code_verifier, redirect_uri."""
    await nur_core(request)
    k = _verwaltetes_konto(connector)
    body = await request.json()
    felder = {"grant_type": "authorization_code"}
    for feld in ("code", "code_verifier", "redirect_uri"):
        wert = body.get(feld)
        if not isinstance(wert, str) or not wert or len(wert) > 2048:
            raise HTTPException(422, f"{feld} fehlt")
        felder[feld] = wert
    return await _einloesen(connector, k, felder)


@app.post("/verwaltung/konto/{connector}/geraetecode")
async def verwaltung_geraetecode(connector: str, request: Request):
    """Microsoft: Gerätecode anfordern. Den device_code behält der Tresor, der Core bekommt nur den Code zum Eintippen."""
    await nur_core(request)
    k = _verwaltetes_konto(connector)
    if not k.get("geraetecode_url"):
        raise HTTPException(409, f"{connector} meldet sich nicht per Gerätecode an")
    name = str(k.get("client_id") or "")
    client_id = speicher.lesen()["geheimnisse"].get(name) or os.environ.get(name, "") if name else ""
    if not client_id:
        raise HTTPException(409, f"{name or 'Die Client-ID'} fehlt")
    async with httpx.AsyncClient(timeout=30) as c:  # über den Egress-Proxy, gleicher Host wie token_url
        r = await c.post(k["geraetecode_url"], data={"client_id": client_id, "scope": str(k.get("scopes", ""))})
    try:
        d = r.json()
    except ValueError:
        d = {"error": f"HTTP {r.status_code}"}
    if "device_code" not in d:
        protokoll(verwaltung="core", connector=connector, konto="geraetecode", ergebnis=d.get("error", "fehler"))
        return {"error": str(d.get("error", "fehler")), "error_description": str(d.get("error_description", ""))[:300]}
    dauer = int(d.get("expires_in", 900))
    geraetecodes[connector] = {"device_code": d["device_code"], "laeuft_ab": time.time() + dauer}
    protokoll(verwaltung="core", connector=connector, konto="geraetecode", ergebnis="ok")
    return {"user_code": d["user_code"], "verification_uri": d.get("verification_uri") or d.get("verification_url"),
            "interval": int(d.get("interval", 5)), "expires_in": dauer}


@app.post("/verwaltung/konto/{connector}/geraetecode/pruefen")
async def verwaltung_geraetecode_pruefen(connector: str, request: Request):
    """Hat der Nutzer den Code beim Anbieter eingegeben? authorization_pending heißt: noch warten."""
    await nur_core(request)
    k = _verwaltetes_konto(connector)
    g = geraetecodes.get(connector)
    if not g or g["laeuft_ab"] < time.time():
        geraetecodes.pop(connector, None)
        return {"error": "expired_token", "error_description": "Der Code ist abgelaufen."}
    antwort = await _einloesen(connector, k, {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                             "device_code": g["device_code"]})
    if antwort.get("error") not in ("authorization_pending", "slow_down"):
        geraetecodes.pop(connector, None)
    return antwort


@app.post("/verwaltung/konto/{connector}/abmelden")
async def verwaltung_abmelden(connector: str, request: Request):
    await nur_core(request)
    if connector not in manifeste():
        raise HTTPException(404, f"Unbekannter Connector {connector}")
    geraetecodes.pop(connector, None)
    await speicher.aendern(lambda d: d["konten"].pop(connector, None))
    protokoll(verwaltung="core", connector=connector, konto="abmelden", ergebnis="ok")
    return {"ok": True}


# ---------------------------------------------------------------- Kommandozeile


def bekannte_namen() -> set[str]:
    namen = set()
    for m in manifeste().values():
        namen |= erlaubte_namen(m)
        k = m.get("konto") or {}
        namen |= {k[x] for x in ("client_id", "client_secret") if k.get(x)}
    return namen


def befehl(argv: list[str]) -> None:
    s = Speicher()
    daten = s.lesen()
    if argv[:1] == ["setzen"] and len(argv) == 2:
        wert = getpass.getpass(f"Wert für {argv[1]}: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
        daten["geheimnisse"][argv[1]] = wert
        s.schreiben(daten)
        print(f"{argv[1]} gespeichert.")
    elif argv[:1] == ["entfernen"] and len(argv) == 2:
        daten["geheimnisse"].pop(argv[1], None)
        s.schreiben(daten)
        print(f"{argv[1]} entfernt.")
    elif argv == ["liste"]:
        for n in sorted(daten["geheimnisse"]):
            print(n)
        for n in sorted(daten["konten"]):
            print(f"Konto {n} (verbunden)")
        fehlt = bekannte_namen() - set(daten["geheimnisse"])
        if fehlt:
            print("In Manifesten genannt, aber nicht im Tresor:", ", ".join(sorted(fehlt)))
    elif argv == ["importieren"]:
        namen = bekannte_namen()
        neu = []
        for zeile in sys.stdin:
            k, _, v = zeile.strip().partition("=")
            if k in namen and v:
                daten["geheimnisse"][k] = v.strip().strip('"').strip("'")
                neu.append(k)
        s.schreiben(daten)
        print("Übernommen:", ", ".join(neu) or "nichts", "\nJetzt diese Zeilen aus der .env löschen.")
    elif argv == ["namen"]:  # Namen aus den Manifesten (für Skripte, z. B. runpod/start.sh)
        print("\n".join(sorted(bekannte_namen())))
    elif argv[:1] == ["konto-importieren"] and len(argv) == 2:  # Token-Datei eines Connectors (RunPod ohne Tresor)
        t = json.loads(sys.stdin.read())
        if not t.get("refresh_token"):
            sys.exit("Kein refresh_token in der Datei")
        daten["konten"][argv[1]] = {"access_token": t.get("access_token", ""), "refresh_token": t["refresh_token"],
                                    "laeuft_ab": t.get("laeuft_ab", 0), "scope": t.get("scope", ""), "seit": t.get("seit")}
        s.schreiben(daten)
        print(f"Konto {argv[1]} übernommen.")
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if len(sys.argv) > 1 and sys.argv[1] != "dienst":
        befehl(sys.argv[1:])
    else:
        import uvicorn

        uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8400")))
