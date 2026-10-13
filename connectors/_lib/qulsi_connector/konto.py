"""OAuth-Konten für Connectoren mit Cloud-Anmeldung (Google, Microsoft).

Mit Tresor (TRESOR_URL gesetzt, Standard auf der Box): Refresh-Token und Client-Secret liegen nur
im Tresor. Der Connector bekommt dort kurzlebige Zugriffstokens; beim Anmelden löst der Tresor den
Code ein. Im Manifest steht dafür "konto" mit token_url (und client_secret als Name im Tresor).

Ohne Tresor (RunPod, Tests): das Token liegt als Datei im Datenordner des Connectors (KONTO_DIR,
nur für ihn lesbar). Erneuert wird es über den Egress-Proxy, deshalb muss der Token-Endpunkt
im Manifest unter network erlaubt sein.

Anmelden ist ein einmaliger Schritt auf der Kommandozeile (siehe README):
  docker compose exec connector-google python google/server.py anmelden
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

from . import _tresor_kopf, intern, internet


class NichtVerbunden(RuntimeError):
    pass


class Konto:
    def __init__(self, name: str, token_url: str, client_id: str, client_secret: str = "", scopes: str = "",
                 verzeichnis: str | Path | None = None):
        self.name = name
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = scopes
        self.pfad = Path(verzeichnis or os.environ.get("KONTO_DIR", "/data/konto")) / f"{name}.json"
        self.tresor = os.environ.get("TRESOR_URL", "")

    async def _tresor(self, methode: str, pfad: str, **kw) -> httpx.Response:
        async with intern(timeout=40) as c:
            return await c.request(methode, f"{self.tresor}{pfad}", headers=_tresor_kopf(), **kw)

    # ------------------------------------------------------------ Datei

    def laden(self) -> dict | None:
        try:
            return json.loads(self.pfad.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def verbunden(self) -> bool:
        if self.tresor:
            try:
                r = httpx.get(f"{self.tresor}/konto/status", headers=_tresor_kopf(), timeout=10, trust_env=False)
                return bool(r.json().get("verbunden"))
            except (httpx.HTTPError, ValueError):
                return False
        return bool((self.laden() or {}).get("refresh_token"))

    def speichern(self, token: dict) -> dict:
        if self.tresor:  # der Tresor hat das Token beim Einlösen schon gespeichert
            return token
        alt = self.laden() or {}
        daten = {
            "access_token": token["access_token"],
            # Google schickt beim Erneuern kein neues refresh_token, Microsoft schon (dann das neue nehmen)
            "refresh_token": token.get("refresh_token") or alt.get("refresh_token"),
            "laeuft_ab": time.time() + int(token.get("expires_in", 3600)),
            "scope": token.get("scope") or alt.get("scope", ""),
        }
        self.pfad.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.pfad.with_suffix(".tmp")
        tmp.write_text(json.dumps(daten), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.pfad)
        return daten

    def abmelden(self) -> None:
        if self.tresor:
            httpx.post(f"{self.tresor}/konto/abmelden", headers=_tresor_kopf(), timeout=10, trust_env=False)
        self.pfad.unlink(missing_ok=True)

    # ------------------------------------------------------------ Token

    async def token_holen(self, **felder) -> dict:
        """Token-Endpunkt aufrufen (Code einlösen, Gerätecode abfragen, erneuern)."""
        daten = {"client_id": self.client_id, **felder}
        if self.tresor:  # Client-Secret und Refresh-Token bleiben im Tresor
            r = await self._tresor("POST", "/konto/token", json=daten)
            try:
                return r.json()
            except ValueError:
                return {"error": f"Tresor HTTP {r.status_code}"}
        if self.client_secret:
            daten["client_secret"] = self.client_secret
        async with internet() as c:
            r = await c.post(self.token_url, data=daten)
        try:
            antwort = r.json()
        except ValueError:
            antwort = {"error": f"HTTP {r.status_code}", "error_description": r.text[:200]}
        if r.status_code >= 400 and "error" not in antwort:
            antwort["error"] = f"HTTP {r.status_code}"
        return antwort

    async def zugang(self, erneuern: bool = False) -> str:
        if self.tresor:
            r = await self._tresor("GET", "/token", params={"erneuern": "1"} if erneuern else None)
            if r.status_code != 200:
                raise NichtVerbunden(f"{self.name}: {r.json().get('detail', r.text) if r.content else r.status_code}")
            return r.json()["access_token"]
        daten = self.laden()
        if not daten or not daten.get("refresh_token"):
            raise NichtVerbunden(f"{self.name} ist nicht verbunden. Einmal anmelden: siehe README, Abschnitt Konten.")
        if erneuern or daten.get("laeuft_ab", 0) - 60 < time.time():
            t = await self.token_holen(grant_type="refresh_token", refresh_token=daten["refresh_token"])
            if "access_token" not in t:
                raise NichtVerbunden(f"{self.name}: Anmeldung abgelaufen ({t.get('error')}). Bitte neu anmelden.")
            daten = self.speichern(t)
        return daten["access_token"]

    async def anfrage(self, methode: str, url: str, **kw) -> httpx.Response:
        """API-Aufruf mit Token. Bei 401 einmal erneuern und wiederholen."""
        for versuch in (0, 1):
            token = await self.zugang(erneuern=bool(versuch))
            headers = {**kw.pop("headers", {}), "Authorization": f"Bearer {token}"}
            async with internet(follow_redirects=True) as c:
                r = await c.request(methode, url, headers=headers, **kw)
            kw["headers"] = {k: v for k, v in headers.items() if k != "Authorization"}
            if r.status_code != 401:
                return r
        return r


def fehlertext(r: httpx.Response) -> str:
    try:
        f = r.json().get("error")
        text = f.get("message") if isinstance(f, dict) else f
    except ValueError:
        text = r.text[:200]
    return f"Fehler {r.status_code}: {text or r.reason_phrase}"
