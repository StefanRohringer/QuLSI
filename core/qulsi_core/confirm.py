"""Freigaben für kritische Aktionen über die Handy-App.

Ablauf:
1. Der Connector Hub legt eine Freigabe mit Einmal-Token an (in der DB steht nur der Hash).
2. ntfy schickt eine Benachrichtigung mit den Knöpfen "Freigeben" und "Ablehnen".
   Die Knöpfe rufen /bestaetigung/<id> mit dem Token auf, über Tailscale.
3. Erst dann führt der Hub das Werkzeug aus, mit genau den gespeicherten
   Argumenten. Das Sprachmodell ist an diesem Schritt nicht beteiligt.

Alternativ in der QuLSI-App (Erlauben/Ablehnen), aber nur mit der Freigabe-PIN
(QULSI_FREIGABE_PIN, nur auf der Box gespeichert). So reicht ein gestohlener
App-Schlüssel nicht, um Aktionen freizugeben. Ohne PIN geht Erlauben nur übers Handy.
Ablehnen geht in der App immer. Falsche PINs sperren die App-Freigabe (AppPin).

Eine Freigabe per Stimme gibt es absichtlich nicht.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Freigabe:
    id: str
    token: str
    url: str
    benachrichtigt: bool


class Freigaben:
    def __init__(self, database_url: str, public_url: str, gueltig_sekunden: int, notifier: "Notifier"):
        self.database_url = database_url
        self.public_url = public_url
        self.gueltig = gueltig_sekunden
        self.notifier = notifier

    async def offen_gleich(self, tool: str, arguments: dict) -> dict | None:
        """Wartet schon eine offene Freigabe für dasselbe Werkzeug mit denselben Argumenten? (Nutzer sagt „ja“ nochmal)"""
        async with await psycopg.AsyncConnection.connect(self.database_url, row_factory=dict_row) as conn:
            cur = await conn.execute(
                """SELECT id, laeuft_ab FROM confirmations WHERE status = 'offen' AND laeuft_ab > now()
                   AND tool = %s AND arguments = %s::jsonb ORDER BY erstellt DESC LIMIT 1""",
                (tool, json.dumps(arguments)))
            return await cur.fetchone()

    async def anlegen(self, tool: str, arguments: dict, kanal: str, belastet: bool, begruendung: str,
                      vorschau: str | None = None) -> Freigabe:
        fid = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        laeuft_ab = datetime.now(timezone.utc) + timedelta(seconds=self.gueltig)
        async with await psycopg.AsyncConnection.connect(self.database_url) as conn:
            await conn.execute(
                """INSERT INTO confirmations (id, token_hash, tool, arguments, begruendung, vorschau, kanal, belastet, laeuft_ab)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (fid, hash_token(token), tool, json.dumps(arguments), begruendung, vorschau, kanal, belastet, laeuft_ab),
            )
        url = f"{self.public_url}/bestaetigung/{fid}?token={token}"
        ok = False
        try:
            await self.notifier.senden_freigabe(fid, tool, arguments, belastet, url, vorschau)
            ok = self.notifier.aktiv  # ohne ntfy nur protokolliert, die Freigabe steht in der App
        except Exception:  # noqa: BLE001
            log.exception("Freigabe %s konnte nicht ans Handy geschickt werden", fid)
        return Freigabe(fid, token, url, ok)

    async def laden(self, fid: str, token: str) -> dict | None:
        """Lädt eine Freigabe nur mit gültigem Token."""
        try:
            uuid.UUID(fid)
        except ValueError:
            return None
        async with await psycopg.AsyncConnection.connect(self.database_url, row_factory=dict_row) as conn:
            cur = await conn.execute("SELECT * FROM confirmations WHERE id = %s", (fid,))
            row = await cur.fetchone()
        if not row or not secrets.compare_digest(row["token_hash"], hash_token(token)):
            return None
        return row

    async def entscheiden(self, fid: str, token: str, freigeben: bool) -> dict | None:
        """Setzt den Status atomar. Liefert die Zeile nur, wenn sie noch offen und gültig war."""
        if await self.laden(fid, token) is None:
            return None
        return await self._setzen(fid, freigeben)

    async def entscheiden_app(self, fid: str, freigeben: bool) -> dict | None:
        """Entscheidung aus der App. Die Freigabe-PIN prüft der Aufrufer vorher (AppPin)."""
        try:
            uuid.UUID(fid)
        except ValueError:
            return None
        return await self._setzen(fid, freigeben)

    async def _setzen(self, fid: str, freigeben: bool) -> dict | None:
        neu = "freigegeben" if freigeben else "abgelehnt"
        async with await psycopg.AsyncConnection.connect(self.database_url, row_factory=dict_row) as conn:
            cur = await conn.execute(
                """UPDATE confirmations SET status = %s, entschieden = now()
                   WHERE id = %s AND status = 'offen' AND laeuft_ab > now()
                   RETURNING *""",
                (neu, fid),
            )
            return await cur.fetchone()

    async def ergebnis(self, fid: str, status: str, text: str) -> None:
        async with await psycopg.AsyncConnection.connect(self.database_url) as conn:
            await conn.execute(
                "UPDATE confirmations SET status = %s, ergebnis = %s WHERE id = %s", (status, text[:4000], fid)
            )


def pin_hash(pin: str, salz: bytes | None = None) -> str:
    """scrypt-Hash im Format scrypt:<salz>:<hash> (ohne $, damit docker compose die .env nicht umdeutet)."""
    salz = salz or secrets.token_bytes(16)
    h = hashlib.scrypt(pin.encode(), salt=salz, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt:{salz.hex()}:{h.hex()}"


def pin_stimmt(gespeichert: str, pin: str) -> bool:
    if gespeichert.startswith("scrypt:"):
        try:
            _, salz, _ = gespeichert.split(":")
            return secrets.compare_digest(pin_hash(pin, bytes.fromhex(salz)), gespeichert)
        except ValueError:
            log.error("QULSI_FREIGABE_PIN hat ein falsches Format")
            return False
    return secrets.compare_digest(pin.encode(), gespeichert.encode())  # Klartext (z. B. RunPod-env)


class PinFehler(Exception):
    def __init__(self, status: int, text: str, daten: dict | None = None):
        super().__init__(text)
        self.status, self.text, self.daten = status, text, daten or {}


class AppPin:
    """Freigabe-PIN für Erlauben in der App, mit Sperre nach Fehlversuchen.

    Nach FEHLER_MAX falschen PINs ist Erlauben in der App gesperrt, jede weitere Sperre doppelt so
    lang (15 min, 30 min, ... bis 16 h). Der Zähler liegt in system_state und übersteht Neustarts.
    """

    FEHLER_MAX = 5
    SPERRE_SEKUNDEN = 900
    KEY = "freigabe_pin"            # Fehlversuche und Sperre
    KEY_HASH = "freigabe_pin_hash"  # die PIN selbst (scrypt), in der App oder per Einrichtungscode gesetzt
    KEY_CODE = "freigabe_pin_code"  # Einrichtungscode von der Box (Hash, gültig bis, Fehlversuche)
    CODE_SEKUNDEN = 900
    CODE_ZEICHEN = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # ohne 0/O und 1/I

    def __init__(self, store: Any, gespeichert: str):
        self.store = store
        self.gespeichert = gespeichert.strip()
        self._lock = asyncio.Lock()  # PINs nacheinander prüfen, sonst zählt paralleles Raten nicht mit

    @property
    def eingerichtet(self) -> bool:
        return bool(self.gespeichert)

    async def _lesen(self, key: str) -> dict | None:
        row = await self.store.eine("SELECT value FROM system_state WHERE key = %s", (key,))
        return (row or {}).get("value")

    async def _schreiben(self, key: str, wert: dict | None) -> None:
        async with await self.store.conn() as conn:
            if wert is None:
                await conn.execute("DELETE FROM system_state WHERE key = %s", (key,))
            else:
                await conn.execute(
                    """INSERT INTO system_state (key, value) VALUES (%s, %s::jsonb)
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, zeit = now()""",
                    (key, json.dumps(wert)))

    async def laden(self) -> None:
        """PIN aus der Datenbank. Eine PIN aus QULSI_FREIGABE_PIN (ältere Einrichtung) wird einmalig übernommen."""
        gespeichert = await self._lesen(self.KEY_HASH)
        if gespeichert is not None:  # auch leer: dann wurde die PIN ausdrücklich entfernt
            self.gespeichert = gespeichert.get("hash") or ""
        elif self.gespeichert:
            alt = self.gespeichert if self.gespeichert.startswith("scrypt:") else pin_hash(self.gespeichert)
            await self._schreiben(self.KEY_HASH, {"hash": alt, "quelle": "env"})
            self.gespeichert = alt

    @staticmethod
    def pin_gueltig(pin: str) -> bool:
        return pin.isdigit() and pin.isascii() and 6 <= len(pin) <= 12

    async def setzen(self, neue_pin: str | None, quelle: str) -> None:
        """Neue PIN speichern (None: PIN entfernen). Fehlversuche und Sperre werden zurückgesetzt."""
        if neue_pin is None:
            await self._schreiben(self.KEY_HASH, {"hash": "", "quelle": quelle})
            self.gespeichert = ""
        else:
            if not self.pin_gueltig(neue_pin):
                raise PinFehler(400, "Die PIN muss aus 6 bis 12 Ziffern bestehen.")
            h = await asyncio.to_thread(pin_hash, neue_pin)
            await self._schreiben(self.KEY_HASH, {"hash": h, "quelle": quelle,
                                                  "zeit": datetime.now(timezone.utc).isoformat()})
            self.gespeichert = h
        await self._speichern({"fehler": 0, "sperren": 0, "gesperrt_bis": None})

    async def code_neu(self) -> str:
        """Einmaliger Einrichtungscode (nur auf der Box sichtbar), 15 Minuten gültig. Ersetzt einen alten."""
        code = "".join(secrets.choice(self.CODE_ZEICHEN) for _ in range(8))
        bis = datetime.now(timezone.utc) + timedelta(seconds=self.CODE_SEKUNDEN)
        await self._schreiben(self.KEY_CODE, {"hash": hashlib.sha256(code.encode()).hexdigest(),
                                              "bis": bis.isoformat(), "fehler": 0})
        return f"{code[:4]}-{code[4:]}"

    async def code_offen(self) -> datetime | None:
        c = await self._lesen(self.KEY_CODE)
        if c and (bis := datetime.fromisoformat(c["bis"])) > datetime.now(timezone.utc):
            return bis
        return None

    async def code_einloesen(self, code: str) -> None:
        """Prüft den Einrichtungscode und verbraucht ihn. Nach FEHLER_MAX falschen Codes ist er ungültig."""
        async with self._lock:
            c = await self._lesen(self.KEY_CODE)
            if not c or datetime.fromisoformat(c["bis"]) <= datetime.now(timezone.utc):
                raise PinFehler(403, "Kein gültiger Einrichtungscode. Einen neuen zeigt die Box "
                                     "(RunPod: bash runpod/qulsi.sh pin).")
            norm = "".join(ch for ch in code.upper() if ch.isalnum())[:16]
            if secrets.compare_digest(hashlib.sha256(norm.encode()).hexdigest(), c["hash"]):
                await self._schreiben(self.KEY_CODE, None)
                return
            c["fehler"] = c.get("fehler", 0) + 1
            if c["fehler"] >= self.FEHLER_MAX:
                await self._schreiben(self.KEY_CODE, None)
                raise PinFehler(429, "Zu viele falsche Einrichtungscodes. Der Code ist jetzt ungültig, "
                                     "bitte auf der Box einen neuen holen.", {"neu_gesperrt": True})
            await self._schreiben(self.KEY_CODE, c)
            raise PinFehler(403, "Falscher Einrichtungscode.", {"versuche_uebrig": self.FEHLER_MAX - c["fehler"]})

    async def zustand(self) -> dict:
        row = await self.store.eine("SELECT value FROM system_state WHERE key = %s", (self.KEY,))
        return {"fehler": 0, "sperren": 0, "gesperrt_bis": None, **((row or {}).get("value") or {})}

    async def _speichern(self, z: dict) -> None:
        async with await self.store.conn() as conn:
            await conn.execute(
                """INSERT INTO system_state (key, value) VALUES (%s, %s::jsonb)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, zeit = now()""",
                (self.KEY, json.dumps(z)))

    @staticmethod
    def gesperrt(z: dict) -> datetime | None:
        bis = z.get("gesperrt_bis")
        if bis and (t := datetime.fromisoformat(bis)) > datetime.now(timezone.utc):
            return t
        return None

    async def pruefen(self, pin: str) -> None:
        """Wirft PinFehler, wenn keine PIN eingerichtet ist, die App-Freigabe gesperrt oder die PIN falsch ist."""
        await self.laden()  # die Datenbank gilt (PIN kann auf der Box entfernt worden sein)
        if not self.eingerichtet:
            raise PinFehler(403, "In der App ist noch keine Freigabe-PIN eingerichtet. Bitte zuerst unter "
                                 "Einstellungen die PIN festlegen (mit dem Einrichtungscode von der Box).")
        async with self._lock:
            z = await self.zustand()
            if bis := self.gesperrt(z):
                raise PinFehler(429, "Zu viele falsche PINs. Freigeben in der App ist vorübergehend gesperrt.",
                                {"gesperrt_bis": bis.isoformat()})
            if pin and len(pin) <= 64 and await asyncio.to_thread(pin_stimmt, self.gespeichert, pin):
                if z["fehler"] or z["sperren"]:
                    await self._speichern({"fehler": 0, "sperren": 0, "gesperrt_bis": None})
                return
            z["fehler"] += 1
            if z["fehler"] >= self.FEHLER_MAX:
                dauer = self.SPERRE_SEKUNDEN * 2 ** min(z["sperren"], 6)
                z = {"fehler": 0, "sperren": z["sperren"] + 1,
                     "gesperrt_bis": (datetime.now(timezone.utc) + timedelta(seconds=dauer)).isoformat()}
                await self._speichern(z)
                raise PinFehler(429, "Zu viele falsche PINs. Freigeben in der App ist vorübergehend gesperrt.",
                                {"gesperrt_bis": z["gesperrt_bis"], "neu_gesperrt": True})
            await self._speichern(z)
            raise PinFehler(403, "Falsche Freigabe-PIN.", {"versuche_uebrig": self.FEHLER_MAX - z["fehler"]})


class Notifier:
    """Schickt Benachrichtigungen per ntfy (selbst gehostet, App für Android und iOS)."""

    def __init__(self, ntfy_url: str, topic: str, token: str):
        self.url = ntfy_url
        self.topic = topic
        self.token = token
        self.gesendet: list[dict] = []  # für Tests und Fehlersuche

    @property
    def aktiv(self) -> bool:
        """ntfy läuft mit deny-all: ohne Adresse und Token kommt nichts an, dann gar nicht erst versuchen.
        NTFY_OHNE_TOKEN=1 nur für den Teststack, dessen ntfy ohne Anmeldung schreiben lässt."""
        return bool(self.url and (self.token or os.environ.get("NTFY_OHNE_TOKEN") == "1"))

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def _post(self, payload: dict) -> None:
        self.gesendet.append(payload)
        if not self.aktiv:
            log.info("ntfy nicht eingerichtet, Benachrichtigung nur in der App: %s", payload.get("title"))
            return
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(self.url, json=payload, headers=self._headers())
            r.raise_for_status()

    async def senden_freigabe(self, fid: str, tool: str, arguments: dict, belastet: bool, url: str,
                              vorschau: str | None = None) -> None:
        details = vorschau or "\n".join(f"{k}: {v}" for k, v in arguments.items())
        warnung = "\n\nAchtung: Im Gespräch standen Inhalte aus Mail, Web oder Dateien." if belastet else ""
        await self._post(
            {
                "topic": self.topic,
                "title": f"Freigabe nötig: {tool}",
                "message": f"{details}{warnung}",
                "priority": 5 if belastet else 4,
                "tags": ["lock"],
                "click": url,
                "actions": [
                    {"action": "http", "label": "Freigeben", "url": f"{url}&entscheid=ja", "method": "POST", "clear": True},
                    {"action": "http", "label": "Ablehnen", "url": f"{url}&entscheid=nein", "method": "POST", "clear": True},
                ],
            }
        )

    async def senden(self, titel: str, text: str) -> None:
        await self._post({"topic": self.topic, "title": titel, "message": text})
