"""Konten und Zugangsdaten in der App einrichten, ohne Terminal und ohne SSH.

Endpunkte (alle mit API-Schlüssel, eingebunden in main.py):
- GET  /api/konten                             je Connector: Konto verbunden? welche Zugangsdaten gesetzt (nur ja/nein)
- POST /api/konten/zugangsdaten                {"connector": "nas", "werte": {"NAS_PASSWORD": "..."}}
- POST /api/konten/zugangsdaten/entfernen      {"connector": "nas", "namen": ["NAS_PASSWORD"]}
- POST /api/konten/{connector}/verbinden       Google- oder Microsoft-Konto verbinden
- GET  /api/konten/{connector}/anmeldung       Stand der Anmeldung (Link bzw. Code zum Eintippen)
- POST /api/konten/{connector}/anmeldung       Google: {"antwort": "<Adresse der Seite 127.0.0.1>"}
- POST /api/konten/{connector}/trennen         Konto auf der Box abmelden

Sicherheit:
- Jede Änderung legt zuerst eine Freigabe an (konto.*). Ausgeführt wird erst nach Erlauben in der App mit
  der Freigabe-PIN oder am Handy (ntfy). Ein gestohlener App-Schlüssel allein ändert also nichts.
- Eingegebene Werte stehen nie in der Freigabe, in der Datenbank, im Protokoll oder in ntfy. Bis zur
  Entscheidung liegen sie nur im Arbeitsspeicher des Core (weg nach Ablauf, Ablehnen oder Neustart),
  danach nur im Tresor. Kein Endpunkt gibt einen Wert zurück.
- Konten: Der Tresor löst den Anmeldecode ein und behält das Refresh-Token. Der Core sieht nie ein Token.
- Änderungen nur aus dem Heimnetz oder über Tailscale (private Adresse des Aufrufers; andere Netze mit
  QULSI_TRESOR_NETZE="192.168.0.0/16,100.64.0.0/10"). QULSI_TRESOR_APP=aus schaltet Änderungen aus der
  App ganz ab, dann nur noch auf der Kommandozeile (docker compose run --rm tresor ...).
- Jede ausgeführte Änderung meldet ntfy aufs Handy ("Warst du das nicht ...").
- RunPod: Tresor wie auf der Box (runpod/start.sh richtet ihn ein und übernimmt die Werte aus der env-Datei).
  Der Schlüssel liegt dort aber auf demselben Volume, deshalb nur Test-Konten. Ohne Tresor (pyrage fehlt)
  landen Werte unverschlüsselt in der env-Datei des Pods.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import ipaddress

import httpx
import yaml
from fastapi import APIRouter, HTTPException, Request

log = logging.getLogger(__name__)

SETZEN, ENTFERNEN, VERBINDEN, TRENNEN = "konto.zugangsdaten", "konto.zugangsdaten_entfernen", "konto.verbinden", "konto.trennen"
WERKZEUGE = {SETZEN, ENTFERNEN, VERBINDEN, TRENNEN}
MAX_WERT = 4096
MAX_OFFEN = 5             # offene Konto-Freigaben gleichzeitig (gegen Spam aufs Handy)
ANMELDUNG_SEKUNDEN = 900  # so lange gilt ein Anmeldelink nach dem Erlauben

# Was die App zu jedem Namen anzeigt. geheim=False: darf als normales Textfeld angezeigt werden.
BESCHREIBUNG = {
    "NAS_PASSWORD": ("Passwort für das NAS", True),
    "IMAP_PASSWORD": ("Passwort für das Mail-Postfach (IMAP)", True),
    "SMTP_PASSWORD": ("Passwort für den Mail-Versand (SMTP), leer lassen = wie IMAP", True),
    "HA_TOKEN": ("Langlebiges Zugriffstoken aus Home Assistant (Profil > Sicherheit)", True),
    "GOOGLE_CLIENT_ID": ("Client-ID des eigenen OAuth-Clients (Google Cloud Console, Typ Desktop-App)", False),
    "GOOGLE_CLIENT_SECRET": ("Client-Secret des OAuth-Clients", True),
    "MS_CLIENT_ID": ("Anwendungs-ID (Client-ID) der App-Registrierung in Microsoft Entra", False),
}

router = APIRouter()

STANDARD_NETZE = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,127.0.0.0/8,::1/128,fc00::/7,fe80::/10"


def _laufzeit() -> str:
    return "runpod" if os.environ.get("QULSI_LAUFZEIT") == "runpod" else "box"


def netz_erlaubt(ip: str) -> bool:
    """Heimnetz oder Tailscale (private Adressen). Auf RunPod kommt alles über dessen Proxy: dort gelten
    nur Freigabe und ntfy-Meldung."""
    if _laufzeit() == "runpod":
        return True
    try:
        netze = [ipaddress.ip_network(n.strip()) for n in (os.environ.get("QULSI_TRESOR_NETZE") or STANDARD_NETZE).split(",")
                 if n.strip()]
        adresse = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(adresse, ipaddress.IPv6Address) and adresse.ipv4_mapped:
        adresse = adresse.ipv4_mapped
    return any(adresse in n for n in netze if n.version == adresse.version)


def app_sperre(request: Request) -> str | None:
    """Warum die App gerade nichts an Zugangsdaten und Konten ändern darf, sonst None."""
    if os.environ.get("QULSI_TRESOR_APP", "an").strip().lower() in ("aus", "off", "false", "0", "nein"):
        return ("Auf dieser Box ist das Ändern von Zugangsdaten in der App abgeschaltet (QULSI_TRESOR_APP=aus). "
                "Bitte auf der Box einrichten.")
    if not netz_erlaubt(request.client.host if request.client else ""):
        return "Zugangsdaten und Konten lassen sich nur aus dem Heimnetz oder über Tailscale ändern."
    return None


async def _pruefen(request: Request, was: str) -> None:
    if grund := app_sperre(request):
        await request.app.state.store.ereignis("sicherheit", f"Konten in der App verweigert ({was}): {grund}")
        raise HTTPException(403, grund)


# ---------------------------------------------------------------- Manifeste


def katalog(verzeichnis: str | Path) -> dict[str, dict]:
    """Connectoren, die Zugangsdaten oder ein Konto brauchen, mit den erlaubten Namen."""
    out = {}
    for pfad in sorted(Path(verzeichnis).glob("*/manifest.yaml")):
        raw = yaml.safe_load(pfad.read_text(encoding="utf-8")) or {}
        name, konto = raw.get("name"), dict(raw.get("konto") or {})
        namen = list(raw.get("secrets") or []) + [konto[x] for x in ("client_id", "client_secret") if konto.get(x)]
        if not name or not (namen or konto):
            continue
        out[name] = {"titel": raw.get("titel", name), "namen": namen, "konto": konto,
                     "anmeldung": konto.get("anmeldung") if konto.get("token_url") else None}
    return out


def _env_ersetzen(text: str, env: dict[str, str]) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: env.get(m.group(1), ""), str(text))


# ---------------------------------------------------------------- Box: Tresor


class TresorZugang:
    """Box: alles geht in den Tresor (age-verschlüsselt). Nur Hineinschreiben, nie Herauslesen."""

    tresor = True

    def __init__(self, url: str):
        self.url = url.rstrip("/")

    async def _anfrage(self, methode: str, pfad: str, **kw) -> dict:
        async with httpx.AsyncClient(timeout=40, trust_env=False) as c:  # Kopf zählt nur auf RunPod (TRESOR_LOKAL)
            r = await c.request(methode, f"{self.url}{pfad}", headers={"X-QuLSI-Connector": "core"}, **kw)
        try:
            daten = r.json()
        except ValueError:
            daten = {}
        if r.status_code >= 400:
            raise RuntimeError(f"Tresor: {daten.get('detail') or r.status_code}")
        return daten

    async def uebersicht(self) -> dict:
        return await self._anfrage("GET", "/verwaltung/uebersicht")

    async def setzen(self, werte: dict[str, str]) -> str:
        await self._anfrage("POST", "/verwaltung/geheimnisse", json={"werte": werte})
        return "verschlüsselt im Tresor gespeichert"

    async def entfernen(self, namen: list[str]) -> str:
        await self._anfrage("POST", "/verwaltung/geheimnisse/entfernen", json={"namen": namen})
        return "aus dem Tresor entfernt"

    async def einloesen(self, connector: str, k: dict, felder: dict) -> dict:
        return await self._anfrage("POST", f"/verwaltung/konto/{connector}/einloesen", json=felder)

    async def geraetecode(self, connector: str, k: dict) -> dict:
        return await self._anfrage("POST", f"/verwaltung/konto/{connector}/geraetecode")

    async def geraetecode_pruefen(self, connector: str, k: dict) -> dict:
        return await self._anfrage("POST", f"/verwaltung/konto/{connector}/geraetecode/pruefen")

    async def abmelden(self, connector: str) -> None:
        await self._anfrage("POST", f"/verwaltung/konto/{connector}/abmelden")


# ---------------------------------------------------------------- RunPod: env-Datei


class PodZugang:
    """RunPod: kein Tresor. Werte in die env-Datei (wie qulsi.sh geheimnis), Tokens als Datei des Connectors.

    Anbieter erreicht der Core hier über den Egress-Proxy im Namen des Connectors (gleiche Ziele wie dessen
    Manifest). Nach neuen Zugangsdaten startet supervisord den betroffenen Connector neu."""

    tresor = False

    def __init__(self, env_datei: str, konten_dir: str, supervisor_conf: str, egress_url: str, katalog_fn):
        self.env_datei = Path(env_datei)
        self.konten_dir = Path(konten_dir)
        self.supervisor_conf = supervisor_conf
        self.egress = urlsplit(egress_url)
        self.katalog = katalog_fn
        self.geraetecodes: dict[str, dict] = {}

    def _env(self) -> dict[str, str]:
        out = {}
        try:
            zeilen = self.env_datei.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return out
        for z in zeilen:
            k, gleich, v = z.partition("=")
            if gleich and re.fullmatch(r"[A-Z][A-Z0-9_]*", k.strip()):
                v = v.strip()
                out[k.strip()] = v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"" else v
        return out

    def _wert(self, name: str) -> str:
        return self._env().get(name) or os.environ.get(name, "")

    def _schreiben(self, aendern) -> None:
        zeilen = self.env_datei.read_text(encoding="utf-8").splitlines() if self.env_datei.exists() else []
        zeilen = aendern(zeilen)
        tmp = self.env_datei.with_name(f"{self.env_datei.name}.{secrets.token_hex(4)}")
        tmp.write_text("\n".join(zeilen) + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.env_datei)

    def _konto_datei(self, connector: str) -> Path:
        return self.konten_dir / connector / f"{connector}.json"

    async def uebersicht(self) -> dict:
        env, kat = self._env(), self.katalog()
        namen = {n for c in kat.values() for n in c["namen"]}
        konten = {}
        for c, info in kat.items():
            if info["anmeldung"]:
                try:
                    d = json.loads(self._konto_datei(c).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                konten[c] = {"verbunden": bool(d.get("refresh_token")), "scope": d.get("scope", ""), "seit": d.get("seit")}
        ids = {c["konto"]["client_id"] for c in kat.values() if c["konto"].get("client_id")}
        return {"geheimnisse": {n: bool(env.get(n) or os.environ.get(n)) for n in sorted(namen)},
                "konten": konten, "client_ids": {n: self._wert(n) for n in sorted(ids)}}

    async def _neu_starten(self, namen: set[str]) -> str:
        betroffen = sorted(c for c, info in self.katalog().items() if namen & set(info["namen"]))
        if not betroffen:
            return ""
        if not self.supervisor_conf:
            return f" Bitte {', '.join(betroffen)} neu starten (bash runpod/qulsi.sh restart connector-<name>)."
        befehl = [sys.executable, "-m", "supervisor.supervisorctl", "-c", self.supervisor_conf, "restart",
                  *[f"connector-{c}" for c in betroffen]]
        r = await asyncio.to_thread(subprocess.run, befehl, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            log.warning("Neustart fehlgeschlagen: %s", (r.stdout + r.stderr)[-300:])
            return f" Neustart von {', '.join(betroffen)} fehlgeschlagen, bitte im Pod neu starten."
        return f" {', '.join(betroffen)} neu gestartet."

    async def setzen(self, werte: dict[str, str]) -> str:
        def aendern(zeilen):
            zeilen = [z for z in zeilen if z.partition("=")[0].strip() not in werte]
            return zeilen + [f"{n}='{w}'" for n, w in werte.items()]

        await asyncio.to_thread(self._schreiben, aendern)
        return "unverschlüsselt in der env-Datei des Pods gespeichert (nur Test-Konten)." + await self._neu_starten(set(werte))

    async def entfernen(self, namen: list[str]) -> str:
        await asyncio.to_thread(self._schreiben, lambda zs: [z for z in zs if z.partition("=")[0].strip() not in namen])
        return "aus der env-Datei des Pods entfernt." + await self._neu_starten(set(namen))

    def _client(self, connector: str) -> httpx.AsyncClient:
        e = self.egress
        return httpx.AsyncClient(timeout=30, trust_env=False,
                                 proxy=f"{e.scheme}://{connector}:x@{e.hostname}:{e.port or 3128}")

    async def _anbieter(self, connector: str, url: str, felder: dict) -> dict:
        async with self._client(connector) as c:
            r = await c.post(url, data=felder)
        try:
            d = r.json()
        except ValueError:
            d = {"error": f"HTTP {r.status_code}"}
        if r.status_code >= 400 and "error" not in d:
            d["error"] = f"HTTP {r.status_code}"
        return d

    async def einloesen(self, connector: str, k: dict, felder: dict) -> dict:
        env = {**os.environ, **self._env()}
        anfrage = dict(felder, client_id=self._wert(k.get("client_id", "")))
        if k.get("client_secret") and self._wert(k["client_secret"]):
            anfrage["client_secret"] = self._wert(k["client_secret"])
        t = await self._anbieter(connector, _env_ersetzen(k["token_url"], env), anfrage)
        if "access_token" not in t:
            return {"error": str(t.get("error", "fehler")), "error_description": str(t.get("error_description", ""))[:300]}
        if not t.get("refresh_token"):
            return {"error": "kein_refresh_token",
                    "error_description": "Der Anbieter hat keinen dauerhaften Zugang erteilt. Bitte neu anmelden."}
        # gleiches Format wie qulsi_connector.konto.Konto.speichern
        daten = {"access_token": t["access_token"], "refresh_token": t["refresh_token"],
                 "laeuft_ab": time.time() + int(t.get("expires_in", 3600)), "scope": t.get("scope", ""),
                 "seit": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        pfad = self._konto_datei(connector)
        pfad.parent.mkdir(parents=True, exist_ok=True)
        tmp = pfad.with_suffix(".tmp")
        tmp.write_text(json.dumps(daten), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(pfad)
        return {"verbunden": True, "scope": daten["scope"]}

    async def geraetecode(self, connector: str, k: dict) -> dict:
        env = {**os.environ, **self._env()}
        client_id = self._wert(k.get("client_id", ""))
        if not client_id:
            raise RuntimeError(f"{k.get('client_id') or 'Die Client-ID'} fehlt")
        d = await self._anbieter(connector, _env_ersetzen(k["geraetecode_url"], env),
                                 {"client_id": client_id, "scope": str(k.get("scopes", ""))})
        if "device_code" not in d:
            return {"error": str(d.get("error", "fehler")), "error_description": str(d.get("error_description", ""))[:300]}
        dauer = int(d.get("expires_in", 900))
        self.geraetecodes[connector] = {"device_code": d["device_code"], "laeuft_ab": time.time() + dauer}
        return {"user_code": d["user_code"], "verification_uri": d.get("verification_uri") or d.get("verification_url"),
                "interval": int(d.get("interval", 5)), "expires_in": dauer}

    async def geraetecode_pruefen(self, connector: str, k: dict) -> dict:
        g = self.geraetecodes.get(connector)
        if not g or g["laeuft_ab"] < time.time():
            self.geraetecodes.pop(connector, None)
            return {"error": "expired_token", "error_description": "Der Code ist abgelaufen."}
        antwort = await self.einloesen(connector, k, {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                                      "device_code": g["device_code"]})
        if antwort.get("error") not in ("authorization_pending", "slow_down"):
            self.geraetecodes.pop(connector, None)
        return antwort

    async def abmelden(self, connector: str) -> None:
        self.geraetecodes.pop(connector, None)
        self._konto_datei(connector).unlink(missing_ok=True)


# ---------------------------------------------------------------- Dienst


class Konten:
    def __init__(self, connectors_dir: str, zugang: TresorZugang | PodZugang | None, gueltig_sekunden: int):
        self.connectors_dir = connectors_dir
        self.zugang = zugang
        self.gueltig = gueltig_sekunden
        self.ausstehend: dict[str, dict] = {}   # Ablage-ID -> eingegebene Werte bis zur Freigabe (nur RAM)
        self.anmeldungen: dict[str, dict] = {}  # Connector -> laufende Anmeldung

    @classmethod
    def aus_umgebung(cls, connectors_dir: str, egress_url: str, gueltig_sekunden: int) -> "Konten":
        laufzeit = os.environ.get("QULSI_LAUFZEIT")
        k = cls(connectors_dir, None, gueltig_sekunden)
        if os.environ.get("TRESOR_URL"):
            k.zugang = TresorZugang(os.environ["TRESOR_URL"])
        elif laufzeit == "runpod" and os.environ.get("QULSI_ENV_FILE"):
            k.zugang = PodZugang(os.environ["QULSI_ENV_FILE"], os.environ.get("QULSI_KONTEN_DIR", "/workspace/qulsi-data/konten"),
                                 os.environ.get("QULSI_SUPERVISOR_CONF", ""), egress_url, k.katalog)
        return k

    def katalog(self) -> dict[str, dict]:
        return katalog(self.connectors_dir)

    def _connector(self, name: str) -> dict:
        kat = self.katalog()
        if name not in kat:
            raise HTTPException(404, f"Unbekannter Connector {name}")
        return kat[name]

    def _zugang(self):
        if self.zugang is None:
            raise HTTPException(503, "Hier gibt es keinen Tresor. Zugangsdaten bitte auf der Box einrichten.")
        return self.zugang

    def _aufraeumen(self) -> None:
        jetzt = time.time()
        for a in [a for a, e in self.ausstehend.items() if e["laeuft_ab"] < jetzt]:
            self.ausstehend.pop(a, None)

    # ------------------------------------------------------------ Eingaben prüfen

    def werte_pruefen(self, connector: str, werte) -> dict[str, str]:
        info = self._connector(connector)
        if not isinstance(werte, dict) or not werte:
            raise HTTPException(422, "Bitte mindestens einen Wert eingeben.")
        sauber = {}
        for n, w in werte.items():
            if n not in info["namen"]:
                raise HTTPException(422, f"{n} gehört nicht zu {info['titel']}.")
            if not isinstance(w, str):
                raise HTTPException(422, f"{n}: Text erwartet.")
            w = w.strip()
            if not w:
                continue  # leeres Feld = nicht ändern
            if len(w) > MAX_WERT or any(ord(z) < 32 for z in w):
                raise HTTPException(422, f"{n}: zu lang oder mit Steuerzeichen.")
            if isinstance(self.zugang, PodZugang) and "'" in w:
                raise HTTPException(422, f"{n}: Auf RunPod darf der Wert kein ' enthalten.")
            sauber[n] = w
        if not sauber:
            raise HTTPException(422, "Bitte mindestens einen Wert eingeben.")
        return sauber

    # ------------------------------------------------------------ Übersicht

    async def uebersicht(self, offen: list[dict]) -> dict:
        laufzeit = "runpod" if os.environ.get("QULSI_LAUFZEIT") == "runpod" else "box"
        stand: dict = {"geheimnisse": {}, "konten": {}, "client_ids": {}}
        fehler = None
        if self.zugang is not None:
            try:
                stand = await self.zugang.uebersicht()
            except Exception as exc:  # noqa: BLE001
                log.warning("Konten-Übersicht nicht verfügbar: %s", exc)
                fehler = "Der Tresor ist gerade nicht erreichbar."
        connectoren = []
        for name, info in self.katalog().items():
            konto = stand["konten"].get(name) or {}
            # Client-IDs können auch aus der .env kommen, dann stehen sie nur unter client_ids
            gesetzt = {n: bool(stand["geheimnisse"].get(n) or stand["client_ids"].get(n)) for n in info["namen"]}
            fuers_konto = [info["konto"][x] for x in ("client_id", "client_secret") if info["konto"].get(x)]
            connectoren.append({
                "name": name, "titel": info["titel"], "anmeldung": info["anmeldung"],
                "verbunden": bool(konto.get("verbunden")) if info["anmeldung"] else None,
                "seit": konto.get("seit"),
                "bereit": all(gesetzt[n] for n in fuers_konto) if info["anmeldung"] else None,
                "zugangsdaten": [{"name": n, "titel": BESCHREIBUNG.get(n, (n, True))[0],
                                  "geheim": BESCHREIBUNG.get(n, (n, True))[1],
                                  "gesetzt": gesetzt[n]} for n in info["namen"]],
                "anmeldung_laeuft": self.anmeldung_stand(name) if name in self.anmeldungen else None,
                "offen": [o for o in offen if o["connector"] == name],
            })
        warnung = None
        if laufzeit == "runpod" and self.zugang is not None and self.zugang.tresor:
            warnung = ("Testbetrieb auf RunPod: Passwörter und Konten liegen verschlüsselt im Tresor, der Schlüssel "
                       "aber im selben Pod-Speicher (/workspace). Bitte nur Test-Konten verbinden.")
        elif laufzeit == "runpod":
            warnung = ("Testbetrieb auf RunPod: Es gibt keinen Tresor. Passwörter und Konten-Anmeldungen liegen "
                       "unverschlüsselt im Pod-Speicher (/workspace). Bitte nur Test-Konten verbinden.")
        if fehler is None and (sperre := getattr(self, "sperre", None)):
            fehler = sperre
        return {"laufzeit": laufzeit, "tresor": bool(self.zugang and self.zugang.tresor),
                "verfuegbar": self.zugang is not None and fehler is None, "fehler": fehler, "warnung": warnung,
                "connectoren": connectoren}

    # ------------------------------------------------------------ Vorbereiten (vor der Freigabe)

    def ablegen(self, werte: dict[str, str]) -> str:
        self._aufraeumen()
        if len(self.ausstehend) >= 20:
            raise HTTPException(429, "Zu viele offene Eingaben. Bitte erst entscheiden oder ablaufen lassen.")
        ablage = str(uuid.uuid4())
        self.ausstehend[ablage] = {"werte": werte, "laeuft_ab": time.time() + self.gueltig + 60}
        return ablage

    def verwerfen(self, args: dict) -> None:
        if args.get("ablage"):
            self.ausstehend.pop(str(args["ablage"]), None)

    # ------------------------------------------------------------ Ausführen (nach der Freigabe)

    async def ausfuehren(self, tool: str, args: dict, store, notifier) -> str:
        try:
            if tool == SETZEN:
                eintrag = self.ausstehend.pop(str(args.get("ablage")), None)
                if not eintrag or eintrag["laeuft_ab"] < time.time():
                    return "Fehler: Die Eingabe ist nicht mehr da (abgelaufen oder QuLSI neu gestartet). Bitte neu eingeben."
                werte = eintrag["werte"]
                if sorted(werte) != sorted(args.get("namen") or []):
                    return "Fehler: Die Eingabe passt nicht zur Freigabe."
                wo = await self._zugang().setzen(werte)
                return f"{', '.join(sorted(werte))} {wo}"
            if tool == ENTFERNEN:
                namen = [n for n in args.get("namen") or [] if n in self._connector(args["connector"])["namen"]]
                return f"{', '.join(namen)} {await self._zugang().entfernen(namen)}"
            if tool == VERBINDEN:
                return await self.anmeldung_starten(args["connector"], store, notifier)
            if tool == TRENNEN:
                c = args["connector"]
                self._anmeldung_beenden(c)
                await self._zugang().abmelden(c)
                await store.ereignis("konto", f"{self._connector(c)['titel']}: Konto auf der Box abgemeldet")
                return (f"{self._connector(c)['titel']} getrennt. Den Zugriff zusätzlich beim Anbieter entfernen "
                        "(Google: myaccount.google.com > Sicherheit, Microsoft: account.microsoft.com > Datenschutz).")
        except HTTPException as exc:
            return f"Fehler: {exc.detail}"
        except Exception as exc:  # noqa: BLE001
            log.exception("Konto-Aktion %s fehlgeschlagen", tool)
            return f"Fehler: {exc}"
        return f"Fehler: unbekannte Aktion {tool}"

    # ------------------------------------------------------------ Anmeldung (Google-Link, Microsoft-Code)

    def _anmeldung_beenden(self, connector: str) -> None:
        a = self.anmeldungen.pop(connector, None)
        if a and a.get("task"):
            a["task"].cancel()

    async def anmeldung_starten(self, connector: str, store, notifier) -> str:
        info = self._connector(connector)
        k = info["konto"]
        self._anmeldung_beenden(connector)
        if info["anmeldung"] == "link":
            stand = await self._zugang().uebersicht()
            client_id = stand["client_ids"].get(k.get("client_id", ""))
            if not client_id:
                return f"Fehler: {k.get('client_id', 'Client-ID')} fehlt. Bitte zuerst unter Zugangsdaten eintragen."
            verifier = secrets.token_urlsafe(64)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            state = secrets.token_urlsafe(24)
            url = k["auth_url"] + "?" + urlencode({
                "client_id": client_id, "redirect_uri": k["redirect_uri"], "response_type": "code",
                "scope": " ".join(str(k.get("scopes", "")).split()), "access_type": "offline", "prompt": "consent",
                "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
            self.anmeldungen[connector] = {"art": "link", "status": "offen", "url": url, "state": state,
                                           "verifier": verifier, "laeuft_ab": time.time() + ANMELDUNG_SEKUNDEN}
            await store.ereignis("konto", f"{info['titel']}: Anmeldung bereit, weiter in der App")
            return f"{info['titel']}: Anmeldung gestartet. In der App den Link öffnen und danach die Adresse einfügen."
        if info["anmeldung"] == "geraetecode":
            d = await self._zugang().geraetecode(connector, k)
            if "user_code" not in d:
                return f"Fehler: Anbieter meldet {d.get('error')} {d.get('error_description', '')}".strip()
            a = {"art": "geraetecode", "status": "offen", "user_code": d["user_code"],
                 "verification_uri": d["verification_uri"], "laeuft_ab": time.time() + d["expires_in"]}
            self.anmeldungen[connector] = a
            a["task"] = asyncio.create_task(self._warten(connector, k, d["interval"], store, notifier))
            await store.ereignis("konto", f"{info['titel']}: Code {d['user_code']} auf {d['verification_uri']} eingeben")
            return f"{info['titel']}: Auf {d['verification_uri']} den Code {d['user_code']} eingeben."
        return f"Fehler: {info['titel']} hat keine Anmeldung über ein Konto."

    async def _warten(self, connector: str, k: dict, pause: int, store, notifier) -> None:
        a = self.anmeldungen.get(connector)
        while a is not None and a["status"] == "offen" and time.time() < a["laeuft_ab"]:
            await asyncio.sleep(pause)
            try:
                t = await self._zugang().geraetecode_pruefen(connector, k)
            except Exception as exc:  # noqa: BLE001
                log.warning("Gerätecode prüfen: %s", exc)
                continue
            if t.get("verbunden"):
                await self._fertig(connector, store, notifier)
                return
            if t.get("error") == "slow_down":
                pause += 5
            elif t.get("error") != "authorization_pending":
                a.update(status="fehler", fehler=f"{t.get('error')} {t.get('error_description', '')}".strip())
                await store.ereignis("konto", f"{self._connector(connector)['titel']}: Anmeldung fehlgeschlagen")
                return
        if a is not None and a["status"] == "offen":
            a.update(status="fehler", fehler="Der Code ist abgelaufen. Bitte neu verbinden.")

    async def _fertig(self, connector: str, store, notifier) -> None:
        titel = self._connector(connector)["titel"]
        a = self.anmeldungen.get(connector) or {}
        a.update(status="verbunden", url=None, state=None, verifier=None, user_code=None)
        await store.ereignis("konto", f"{titel}: Konto verbunden")
        try:
            await notifier.senden("Konto verbunden", f"{titel} ist jetzt mit QuLSI verbunden.")
        except Exception:  # noqa: BLE001
            log.warning("Meldung an ntfy fehlgeschlagen")

    def anmeldung_stand(self, connector: str) -> dict:
        a = self.anmeldungen.get(connector)
        if not a:
            return {"status": "keine"}
        if a["status"] == "offen" and a["laeuft_ab"] < time.time():
            a.update(status="fehler", fehler="Die Anmeldung ist abgelaufen. Bitte neu verbinden.")
        out = {"art": a["art"], "status": a["status"], "fehler": a.get("fehler"),
               "laeuft_ab_iso": datetime.fromtimestamp(a["laeuft_ab"], timezone.utc).isoformat()}
        if a["status"] == "offen":
            out.update({"url": a.get("url")} if a["art"] == "link" else
                       {"user_code": a["user_code"], "verification_uri": a["verification_uri"]})
        return out

    async def link_abschliessen(self, connector: str, antwort: str, store, notifier) -> dict:
        info = self._connector(connector)
        a = self.anmeldungen.get(connector)
        if not a or a["art"] != "link" or a["status"] != "offen" or a["laeuft_ab"] < time.time():
            raise HTTPException(409, "Keine offene Anmeldung. Bitte zuerst verbinden und freigeben.")
        antwort = (antwort or "").strip()
        if "://" not in antwort or len(antwort) > 4096:
            raise HTTPException(422, "Bitte die komplette Adresse aus der Adresszeile einfügen (beginnt mit http://127.0.0.1).")
        q = parse_qs(urlsplit(antwort).query)
        if not secrets.compare_digest(q.get("state", [""])[0], a["state"]):
            raise HTTPException(422, "Die Adresse gehört nicht zu dieser Anmeldung. Bitte den Link aus der App verwenden.")
        if "error" in q:
            a.update(status="fehler", fehler=f"Anbieter meldet: {q['error'][0]}")
            raise HTTPException(422, a["fehler"])
        if not q.get("code"):
            raise HTTPException(422, "In der Adresse fehlt der Code.")
        verifier = a["verifier"]
        a["status"] = "einloesen"  # nur ein Versuch pro Link
        t = await self._zugang().einloesen(connector, info["konto"], {
            "code": q["code"][0], "code_verifier": verifier, "redirect_uri": info["konto"]["redirect_uri"]})
        if not t.get("verbunden"):
            a.update(status="fehler", fehler=f"{t.get('error')} {t.get('error_description', '')}".strip())
            raise HTTPException(422, f"Anmeldung fehlgeschlagen: {a['fehler']}")
        await self._fertig(connector, store, notifier)
        return {"status": "verbunden", "scope": t.get("scope", "")}


# ---------------------------------------------------------------- HTTP


def dienst(request: Request) -> Konten:
    st = request.app.state
    if getattr(st, "konten", None) is None:
        st.konten = Konten.aus_umgebung(st.settings.connectors_dir, st.settings.egress_url, st.freigaben.gueltig)
    return st.konten


async def _offen(store) -> list[dict]:
    rows = await store.alle(
        """SELECT id, tool, arguments FROM confirmations
           WHERE tool LIKE 'konto.%%' AND status = 'offen' AND laeuft_ab > now() ORDER BY erstellt DESC""")
    out = []
    for r in rows:
        args = r["arguments"] if isinstance(r["arguments"], dict) else json.loads(r["arguments"])
        out.append({"id": str(r["id"]), "tool": r["tool"], "connector": args.get("connector")})
    return out


async def _freigabe(request: Request, tool: str, args: dict, begruendung: str, vorschau: str) -> dict:
    st = request.app.state
    await _pruefen(request, vorschau.splitlines()[0])
    if len(await _offen(st.store)) >= MAX_OFFEN:
        raise HTTPException(429, "Es warten schon mehrere Konto-Freigaben. Bitte erst diese entscheiden.")
    fr = await st.freigaben.anlegen(tool, args, "app", False, begruendung, vorschau)
    await st.hub.freigabe_protokoll({"tool": tool, "arguments": args, "kanal": "app", "belastet": False},
                                    "bestaetigung", f"Freigabe {fr.id}")
    await st.store.ereignis("freigabe", f"Wartet auf Freigabe: {vorschau.splitlines()[0]}")
    pin = bool(getattr(getattr(st, "app_pin", None), "eingerichtet", False))
    return {"status": "freigabe_noetig", "freigabe": fr.id, "handy": fr.benachrichtigt, "pin": pin}


@router.get("/api/konten")
async def konten_uebersicht(request: Request):
    k = dienst(request)
    k.sperre = app_sperre(request)
    return await k.uebersicht(await _offen(request.app.state.store))


@router.post("/api/konten/zugangsdaten")
async def zugangsdaten_setzen(request: Request, body: dict):
    k = dienst(request)
    k._zugang()
    connector = str(body.get("connector") or "")
    werte = k.werte_pruefen(connector, body.get("werte"))
    info = k._connector(connector)
    ablage = k.ablegen(werte)
    namen = sorted(werte)
    vorschau = (f"Zugangsdaten für {info['titel']} speichern: {', '.join(namen)}\n"
                f"Die Werte werden nicht angezeigt und landen {'im Tresor' if k.zugang.tresor else 'in der env-Datei des Pods'}.")
    try:
        return await _freigabe(request, SETZEN, {"connector": connector, "namen": namen, "ablage": ablage},
                               "Zugangsdaten aus der App", vorschau)
    except Exception:
        k.verwerfen({"ablage": ablage})
        raise


@router.post("/api/konten/zugangsdaten/entfernen")
async def zugangsdaten_entfernen(request: Request, body: dict):
    k = dienst(request)
    k._zugang()
    connector = str(body.get("connector") or "")
    info = k._connector(connector)
    namen = body.get("namen")
    if not isinstance(namen, list) or not namen or any(n not in info["namen"] for n in namen):
        raise HTTPException(422, f"Bitte nur Namen von {info['titel']} angeben.")
    return await _freigabe(request, ENTFERNEN, {"connector": connector, "namen": sorted(namen)},
                           "Zugangsdaten entfernen (App)", f"Zugangsdaten für {info['titel']} löschen: {', '.join(sorted(namen))}")


@router.post("/api/konten/{connector}/verbinden")
async def verbinden(request: Request, connector: str):
    k = dienst(request)
    k._zugang()
    info = k._connector(connector)
    if not info["anmeldung"]:
        raise HTTPException(409, f"{info['titel']} braucht keine Anmeldung, nur Zugangsdaten.")
    wie = "Link im Browser" if info["anmeldung"] == "link" else "Code auf der Seite von Microsoft"
    return await _freigabe(request, VERBINDEN, {"connector": connector}, "Konto verbinden (App)",
                           f"{info['titel']} mit QuLSI verbinden\nDanach Anmeldung in der App ({wie}).")


@router.get("/api/konten/{connector}/anmeldung")
async def anmeldung(request: Request, connector: str):
    k = dienst(request)
    k._connector(connector)
    return k.anmeldung_stand(connector)


@router.post("/api/konten/{connector}/anmeldung")
async def anmeldung_abschliessen(request: Request, connector: str, body: dict):
    st = request.app.state
    await _pruefen(request, f"Anmeldung {connector} abschließen")
    return await dienst(request).link_abschliessen(connector, str(body.get("antwort") or ""), st.store, st.notifier)


@router.post("/api/konten/{connector}/trennen")
async def trennen(request: Request, connector: str):
    k = dienst(request)
    k._zugang()
    info = k._connector(connector)
    if not info["anmeldung"]:
        raise HTTPException(409, f"{info['titel']} hat kein Konto zum Trennen.")
    return await _freigabe(request, TRENNEN, {"connector": connector}, "Konto trennen (App)",
                           f"{info['titel']} von QuLSI trennen (Zugang auf der Box löschen)")


# ---------------------------------------------------------------- Aufrufe aus main.py (Freigabe entschieden)


def zustaendig(tool: str) -> bool:
    return tool in WERKZEUGE


def abgelehnt(app_state, args: dict) -> None:
    k = getattr(app_state, "konten", None)
    if k is not None:
        k.verwerfen(args)


async def nach_freigabe(app_state, row: dict) -> str:
    args = row["arguments"] if isinstance(row["arguments"], dict) else json.loads(row["arguments"])
    if getattr(app_state, "konten", None) is None:
        app_state.konten = Konten.aus_umgebung(app_state.settings.connectors_dir, app_state.settings.egress_url,
                                               app_state.freigaben.gueltig)
    ergebnis = await app_state.konten.ausfuehren(row["tool"], args, app_state.store, app_state.notifier)
    await app_state.hub.freigabe_protokoll(row, "ausgefuehrt_nach_freigabe", ergebnis)
    if row["tool"] != VERBINDEN and not ergebnis.startswith("Fehler"):  # Verbinden meldet sich nach der Anmeldung selbst
        try:
            await app_state.notifier.senden("Zugangsdaten geändert", f"{ergebnis[:300]}\nWarst du das nicht: "
                                            "Freigabe-PIN und App-Schlüssel ändern.")
        except Exception:  # noqa: BLE001
            log.warning("ntfy-Meldung zur Änderung fehlgeschlagen")
    return ergebnis
