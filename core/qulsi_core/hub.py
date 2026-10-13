"""Connector Hub: einziger Weg vom Sprachmodell zu Werkzeugen.

- Jeder Connector hat ein Manifest (connectors/<name>/manifest.yaml) mit Fähigkeiten,
  erlaubten Internet-Zielen und Stufen (low, medium, high).
- Der Hub fragt den MCP-Server des Connectors nach seinen Werkzeugen und übernimmt nur
  die, die im Manifest stehen. Was ein Connector zusätzlich anbietet, wird ignoriert.
- Jeder Aufruf geht durch die Policy Engine. Erlaubte Aufrufe laufen sofort, kritische
  landen als Freigabe auf dem Handy, alles steht im audit_log, Aktionen im Event Bus.
- Offline-Modus: Ohne Internet bietet der Hub nur Werkzeuge an, die laut Manifest
  offline funktionieren (z. B. Kalender aus dem lokalen Cache).
- Modus je Connector: an, nur_lesen (nur Stufe low) oder aus. Voreinstellung in
  config/policy.yaml > connectoren, umschalten in der App (wird in system_state gespeichert).
- Quarantäne: Sperrt der Egress-Proxy einen Connector wegen auffälligem Verhalten, blendet der Hub
  seine Werkzeuge aus und meldet es aufs Handy, bis du ihn in der App wieder freigibst.
- Signatur: Passt ein Connector-Ordner nicht zu connectors/SIGNATUR.json, wird er nicht geladen.

Für das Sprachmodell heißen Fähigkeiten mit Unterstrich (email_send statt email.send),
weil OpenAI-Funktionsnamen keinen Punkt erlauben.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml
from mcp import Client

from . import schutz
from .confirm import Freigaben
from .integritaet import pruefen as signatur_pruefen
from .memory import Store
from .policy import Entscheid, Kontext, Policy, Stufe, stufe_aus_manifest
from .tools import Kern

FREIGABE_HINWEIS = "Bitte in der App unter Freigaben bestätigen."

log = logging.getLogger(__name__)

MAX_ERGEBNIS = 6000  # Zeichen, damit ein Werkzeug den Kontext nicht flutet


def funktionsname(faehigkeit: str) -> str:
    return faehigkeit.replace(".", "_")


@dataclass
class Manifest:
    name: str
    titel: str
    url: str
    capabilities: list[str]
    allowed: list[str] = field(default_factory=list)
    permissions: dict[str, str] = field(default_factory=dict)
    untrusted: bool | list[str] = False
    offline: bool | list[str] = False
    dienst: str = ""
    vorschau: dict[str, str] = field(default_factory=dict)  # Fähigkeit -> internes Werkzeug für die Freigabe
    allowed_domains: list[str] = field(default_factory=list)
    allowed_ports: list[int] = field(default_factory=lambda: [443])
    secrets: list[str] = field(default_factory=list)  # Namen der Geheimnisse im Tresor
    konto: dict = field(default_factory=dict)  # OAuth: token_url, client_id, client_secret (Namen im Tresor)
    limits: dict = field(default_factory=dict)
    ordner: str = ""

    @classmethod
    def laden(cls, pfad: Path) -> "Manifest":
        raw = yaml.safe_load(pfad.read_text(encoding="utf-8")) or {}
        name = raw["name"]
        url = os.environ.get(f"QULSI_CONNECTOR_{name.upper()}_URL") or raw.get("url", "")
        return cls(
            name=name,
            titel=raw.get("titel", name),
            url=url,
            capabilities=list(raw.get("capabilities") or []),
            allowed=list((raw.get("network") or {}).get("allowed") or []),
            permissions=dict(raw.get("permissions") or {}),
            untrusted=raw.get("untrusted", False),
            offline=raw.get("offline", False),
            dienst=raw.get("dienst", f"connector-{name}"),
            vorschau=dict(raw.get("vorschau") or {}),
            allowed_domains=list((raw.get("network") or {}).get("allowed_domains") or []),
            allowed_ports=[int(p) for p in (raw.get("network") or {}).get("allowed_ports") or [443]],
            secrets=list(raw.get("secrets") or []),
            konto=dict(raw.get("konto") or {}),
            limits=dict(raw.get("limits") or {}),
            ordner=pfad.parent.name,
        )

    def _trifft(self, wert: bool | list[str], faehigkeit: str) -> bool:
        return faehigkeit in wert if isinstance(wert, list) else bool(wert)

    def offline_faehig(self, faehigkeit: str) -> bool:
        return self._trifft(self.offline, faehigkeit)

    def belastend(self, faehigkeit: str) -> bool:
        return self._trifft(self.untrusted, faehigkeit)


def manifeste_laden(verzeichnis: str | Path) -> dict[str, Manifest]:
    out: dict[str, Manifest] = {}
    for pfad in sorted(Path(verzeichnis).glob("*/manifest.yaml")):
        try:
            m = Manifest.laden(pfad)
        except Exception:  # noqa: BLE001
            log.exception("Manifest %s ist ungültig", pfad)
            continue
        if os.environ.get(f"QULSI_CONNECTOR_{m.name.upper()}", "an").lower() in ("aus", "off", "false", "0"):
            log.info("Connector %s ist abgeschaltet", m.name)
            continue
        out[m.name] = m
    return out


@dataclass
class Werkzeug:
    faehigkeit: str  # email.send
    connector: str  # "kern" oder Name aus dem Manifest
    beschreibung: str
    schema: dict
    url: str | None = None  # MCP-Server, None bei Kern-Werkzeugen
    offline: bool = True

    @property
    def funktion(self) -> str:
        return funktionsname(self.faehigkeit)


URL_RE = re.compile(r"(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})(?::\d+)?(?:/|\b)", re.I)


def eigene_hosts() -> list[str]:
    """Wie im Egress (egress/qulsi_egress.py): Adressen, unter denen QuLSI selbst erreichbar ist."""
    hosts = [h.lower() for n in ("PUBLIC_URL", "NTFY_PUBLIC_URL") if (h := urlsplit(os.environ.get(n, "")).hostname)]
    hosts += [h.strip().lower() for h in os.environ.get("QULSI_EIGENE_HOSTS", "").split(",") if h.strip()]
    if pod := os.environ.get("RUNPOD_POD_ID", "").strip().lower():
        hosts.append(f"{pod}-*.proxy.runpod.net")
    return [h for h in hosts if h not in ("localhost", "qulsi.local")]


def zeigt_auf_qulsi(arguments: dict, eigene: list[str]) -> str | None:
    """Ein Werkzeug soll die eigene Adresse von QuLSI öffnen (App, API, ntfy)? Gibt den Host zurück."""
    if not eigene:
        return None
    for wert in arguments.values():
        if isinstance(wert, str):
            for host in URL_RE.findall(wert):
                if any(fnmatch.fnmatchcase(host.lower(), h) for h in eigene):
                    return host
    return None


class Hub:
    def __init__(self, connectors_dir: str, policy: Policy, freigaben: Freigaben, store: Store, kern: Kern,
                 offline_modus: str = "auto", egress_url: str = ""):
        self.connectors_dir = connectors_dir
        self.policy = policy
        self.freigaben = freigaben
        self.store = store
        self.kern = kern
        self.eigene = eigene_hosts()
        self.app_freigabe = False  # Freigabe-PIN eingerichtet: Freigaben gehen auch in der App (main.py setzt das)
        self.offline_modus = offline_modus  # auto | true | false
        self.egress_url = egress_url.rstrip("/")
        self.manifeste: dict[str, Manifest] = {}
        self.werkzeuge: dict[str, Werkzeug] = {}  # Schlüssel: Fähigkeit mit Punkt
        self.erreichbar: dict[str, bool] = {}
        self._offline_cache: tuple[float, bool] = (0.0, False)
        self.modi: dict[str, str] = dict(policy.connector_modi)  # an | nur_lesen | aus
        self.quarantaene: dict[str, dict] = {}
        self.signatur: dict[str, str] = {}  # Connector -> Grund, wenn gesperrt
        self.signatur_pub = os.environ.get("QULSI_SIGNATUR_PUB", "")
        self.egress_status: dict = {}

    # ------------------------------------------------------------------ Laden

    async def laden(self) -> None:
        """Liest Manifeste, registriert Stufen und fragt jeden Connector nach seinen Werkzeugen."""
        gefunden: dict[str, Werkzeug] = {}
        for w in self.kern.werkzeuge.values():
            self.policy.registrieren(w.name, w.stufe)
            gefunden[w.name] = Werkzeug(w.name, "kern", w.beschreibung, w.schema)

        self.manifeste = manifeste_laden(self.connectors_dir)
        await self._modi_laden()
        fehler, alle = signatur_pruefen(self.connectors_dir, self.signatur_pub)
        neu = {m.name: alle or fehler[m.ordner] for m in self.manifeste.values() if alle or m.ordner in fehler}
        for name in set(neu) - set(self.signatur):
            log.error("Signatur: Connector %s gesperrt (%s)", name, neu[name])
            await self._ereignis("sicherheit", f"{name} gesperrt: {neu[name]}", "core")
        self.signatur = neu
        for m in self.manifeste.values():
            for cap in m.capabilities:
                self.policy.registrieren(cap, stufe_aus_manifest(cap, m.permissions), m.belastend(cap))
            if m.name in self.signatur:
                continue
            try:
                async with Client(m.url) as client:
                    result = await client.list_tools()
            except Exception as exc:  # noqa: BLE001
                log.warning("Connector %s (%s) nicht erreichbar: %s", m.name, m.url, exc)
                self.erreichbar[m.name] = False
                continue
            self.erreichbar[m.name] = True
            erlaubt = {funktionsname(c): c for c in m.capabilities}
            for tool in result.tools:
                cap = erlaubt.get(tool.name) or (tool.name if tool.name in m.capabilities else None)
                if cap is None:
                    if tool.name in m.vorschau.values():
                        continue
                    log.warning("Connector %s bietet %s an, das steht nicht im Manifest: ignoriert", m.name, tool.name)
                    continue
                gefunden[cap] = Werkzeug(cap, m.name, tool.description or "", tool.input_schema or {"type": "object"},
                                         m.url, m.offline_faehig(cap))
            fehlt = set(m.capabilities) - {c for c, w in gefunden.items() if w.connector == m.name}
            if fehlt:
                log.warning("Connector %s: im Manifest, aber nicht angeboten: %s", m.name, sorted(fehlt))
        self.werkzeuge = gefunden
        log.info("Werkzeuge geladen: %s", ", ".join(sorted(gefunden)))

    def alle_erreichbar(self) -> bool:
        return all(self.erreichbar.get(n, False) for n in self.manifeste if n not in self.signatur)

    # ------------------------------------------------------------------ Modus, Quarantäne, Signatur

    async def _modi_laden(self) -> None:
        try:
            row = await self.store.eine("SELECT value FROM system_state WHERE key = 'connector_modi'")
        except Exception:  # noqa: BLE001
            row = None
        self.modi = {**self.policy.connector_modi, **((row or {}).get("value") or {})}

    async def modus_setzen(self, name: str, modus: str) -> None:
        if name not in self.manifeste or modus not in ("an", "nur_lesen", "aus"):
            raise ValueError("Unbekannter Connector oder Modus (an, nur_lesen, aus)")
        self.modi[name] = modus
        async with await self.store.conn() as conn:
            await conn.execute(
                """INSERT INTO system_state (key, value) VALUES ('connector_modi', %s::jsonb)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, zeit = now()""",
                (json.dumps(self.modi),))
        await self._ereignis("sicherheit", f"Connector {name}: Modus {modus}", "core")

    def gesperrt(self, w: Werkzeug) -> str | None:
        """Warum ein Werkzeug gerade nicht angeboten wird, sonst None."""
        if w.connector == "kern":
            return None
        if w.connector in self.signatur:
            return f"{w.connector} ist gesperrt: {self.signatur[w.connector]}"
        if w.connector in self.quarantaene:
            return f"{w.connector} ist in Quarantäne ({self.quarantaene[w.connector].get('grund', '')})"
        modus = self.modi.get(w.connector, "an")
        if modus == "aus":
            return f"{w.connector} ist abgeschaltet"
        if modus == "nur_lesen" and self.policy.stufe(w.faehigkeit) is not Stufe.LOW:
            return f"{w.connector} steht auf nur Lesen"
        return None

    async def sicherheit_pruefen(self) -> None:
        """Quarantäne-Stand vom Egress-Proxy holen; neue Fälle melden."""
        if not self.egress_url:
            return
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as c:
                self.egress_status = (await c.get(f"{self.egress_url}/__qulsi/status")).json()
        except Exception:  # noqa: BLE001
            return
        neu = self.egress_status.get("quarantaene") or {}
        for name in set(neu) - set(self.quarantaene):
            text = f"{name} in Quarantäne: {neu[name].get('grund', '')}. Freigeben in der App unter Connectoren."
            log.error(text)
            await self._ereignis("sicherheit", text, "core", {"connector": name})
            try:
                await self.freigaben.notifier.senden("QuLSI: Connector gesperrt", text)
            except Exception:  # noqa: BLE001
                log.exception("Quarantäne-Hinweis nicht gesendet")
        self.quarantaene = neu

    async def quarantaene_aufheben(self, name: str) -> bool:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as c:
            r = await c.post(f"{self.egress_url}/__qulsi/freigeben/{name}")
        ok = bool(r.json().get("freigegeben"))
        if ok:
            self.quarantaene.pop(name, None)
            await self._ereignis("sicherheit", f"{name}: Quarantäne aufgehoben", "core")
        return ok

    def connector_uebersicht(self) -> list[dict]:
        dns = self.egress_status.get("dns_nicht_aufloesbar") or {}
        return [{"name": m.name, "titel": m.titel, "modus": self.modi.get(m.name, "an"),
                 "erreichbar": self.erreichbar.get(m.name, False),
                 "quarantaene": self.quarantaene.get(m.name), "signatur": self.signatur.get(m.name),
                 "dns_fehlt": dns.get(m.name, []),
                 "werkzeuge": sorted(c for c, w in self.werkzeuge.items() if w.connector == m.name),
                 "angeboten": sorted(c for c, w in self.werkzeuge.items()
                                     if w.connector == m.name and not self.gesperrt(w))}
                for m in self.manifeste.values()]

    # ------------------------------------------------------------------ Offline

    async def offline(self) -> bool:
        if self.offline_modus == "true":
            return True
        if self.offline_modus == "false" or not self.egress_url:
            return False
        stand, wert = self._offline_cache
        if time.monotonic() - stand < 30:
            return wert
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as c:
                r = await c.get(f"{self.egress_url}/__qulsi/internet")
                wert = not r.json().get("internet", False)
        except Exception:  # noqa: BLE001
            wert = True
        if wert != self._offline_cache[1]:
            await self._ereignis("offline" if wert else "online", "Offline-Modus an" if wert else "Wieder online", "core")
        self._offline_cache = (time.monotonic(), wert)
        return wert

    # ------------------------------------------------------------------ Für das Modell

    def verfuegbar(self, offline: bool) -> list[Werkzeug]:
        return [w for w in self.werkzeuge.values()
                if self.policy.sichtbar(w.faehigkeit) and (w.offline or not offline) and not self.gesperrt(w)]

    def openai_tools(self, offline: bool = False) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": w.funktion,
                    "description": f"{w.beschreibung} [Stufe: {self.policy.stufe(w.faehigkeit).value}]",
                    "parameters": w.schema,
                },
            }
            for w in self.verfuegbar(offline)
        ]

    def finden(self, name: str) -> Werkzeug | None:
        """Akzeptiert email_send und email.send."""
        if name in self.werkzeuge:
            return self.werkzeuge[name]
        return next((w for w in self.werkzeuge.values() if w.funktion == name), None)

    # ------------------------------------------------------------------ Aufrufen

    async def aufrufen(self, name: str, arguments: dict, ctx: Kontext) -> str:
        """Wird vom Agenten für jeden Werkzeugaufruf des Modells benutzt."""
        w = self.finden(name)
        if w is None or not self.policy.sichtbar(w.faehigkeit):
            await self._audit(name, None, "unbekannt", "abgelehnt", ctx, arguments, "Werkzeug unbekannt")
            return f"Fehler: Das Werkzeug {name} gibt es nicht."
        if grund := self.gesperrt(w):
            await self._audit(w.faehigkeit, w.connector, self.policy.stufe(w.faehigkeit).value, "abgelehnt", ctx, arguments, grund)
            return f"Abgelehnt: {grund}."
        if host := zeigt_auf_qulsi(arguments, self.eigene):
            await self._audit(w.faehigkeit, w.connector, self.policy.stufe(w.faehigkeit).value, "abgelehnt", ctx,
                              arguments, f"eigene Adresse {host}")
            return (f"Abgelehnt: {host} ist QuLSI selbst. Die eigene App, API und Datenbank sind für Werkzeuge tabu. "
                    "Sag dem Nutzer, dass du darauf bewusst nicht zugreifst.")
        if not w.offline and await self.offline():
            return f"{w.faehigkeit} braucht Internet, QuLSI ist gerade offline."

        stufe, entscheid, grund = self.policy.entscheide(w.faehigkeit, ctx, arguments)
        # Prompt-Injection: hohes Risiko oder Empfänger/URL nur aus fremdem Text -> Freigabe (nur strenger)
        entscheid, grund = schutz.verschaerfen(w.faehigkeit, stufe, entscheid, grund, arguments, ctx)
        if entscheid is not Entscheid.ABGELEHNT and (limit := self.policy.zaehlen(w.faehigkeit)):
            await self._audit(w.faehigkeit, w.connector, stufe.value, "abgelehnt", ctx, arguments, limit)
            await self._ereignis("aktion", f"Abgelehnt: {limit}", "core")
            return f"Abgelehnt von der Policy Engine: {limit} Sag dem Nutzer, dass es später wieder geht."

        if entscheid is Entscheid.ABGELEHNT:
            await self._audit(w.faehigkeit, w.connector, stufe.value, "abgelehnt", ctx, arguments, grund)
            return f"Abgelehnt von der Policy Engine: {grund} Sag dem Nutzer, dass er das selbst auslösen muss."

        if entscheid is Entscheid.BESTAETIGUNG:
            try:
                schon = await self.freigaben.offen_gleich(w.faehigkeit, arguments)
            except Exception:  # noqa: BLE001
                schon = None
            if schon:
                return ("Noch nicht ausgeführt. Genau diese Aktion wartet schon auf eine Freigabe in der App, "
                        f"eine zweite wurde nicht angelegt. Sag dem Nutzer wörtlich: „{FREIGABE_HINWEIS}“ "
                        "Eine Bestätigung per Stimme oder Chat zählt nicht.")
            vorschau = await self._vorschau(w, arguments)
            f = await self.freigaben.anlegen(w.faehigkeit, arguments, ctx.kanal, ctx.belastet, grund, vorschau)
            await self._audit(w.faehigkeit, w.connector, stufe.value, "bestaetigung", ctx, arguments, f"Freigabe {f.id}")
            await self._ereignis("freigabe", f"Freigabe angefragt: {w.faehigkeit}", "core", {"id": f.id})
            minuten = self.policy.gueltig_sekunden // 60
            pin = "" if self.app_freigabe else (" Zum Erlauben braucht es eine Freigabe-PIN; die ist noch nicht "
                                                "eingerichtet (in der App unter Einstellungen).")
            handy = " Zusätzlich ging eine Benachrichtigung ans Handy." if f.benachrichtigt else ""
            return (
                f"Noch nicht ausgeführt. Die Aktion wartet auf eine Freigabe in der App (gültig {minuten} Minuten).{handy}{pin} "
                f"Sag dem Nutzer wörtlich: „{FREIGABE_HINWEIS}“ Erst dann läuft die Aktion. "
                "Eine Bestätigung per Stimme oder Chat zählt nicht."
            )

        text = await self.ausfuehren(w, arguments)
        # Kern-Werkzeuge wie memory.find melden selbst, wenn ihr Ergebnis Text aus Mail/Datei/Kalender enthält
        fremd = w.connector == "kern" and text.startswith('{"fremder_inhalt": true')
        if self.policy.belastet_nach(w.faehigkeit) or fremd:
            ctx.belastet = True
            ctx.belastet_durch.append(w.faehigkeit)
            # Versteckten Text entfernen, Anweisungen an QuLSI erkennen (schutz/injektion.py)
            text, befund = schutz.pruefen_ergebnis(w.faehigkeit, text, ctx)
            if befund.risiko in ("mittel", "hoch"):
                await self._audit("schutz.injektion", w.connector, befund.risiko, "erkannt", ctx,
                                  {"quelle": w.faehigkeit, "argumente": arguments}, json.dumps(befund.als_dict(),
                                                                                          ensure_ascii=False)[:500])
            if befund.risiko == "hoch":
                log.warning("Prompt-Injection in %s erkannt: %s", w.faehigkeit, "; ".join(befund.gruende))
                await self._ereignis("warnung", schutz.warnung(befund, w.faehigkeit), _quelle(w))
        else:
            schutz.eigene_ziele_aufnehmen(text, ctx)  # Adressen aus Kontakten/eigenen Daten sind bekannt
        await self._audit(w.faehigkeit, w.connector, stufe.value, "erlaubt", ctx, arguments, text[:500])
        if stufe is not Stufe.LOW and (zeile := _aktion_text(w.faehigkeit, arguments, text)):
            await self._ereignis("aktion", zeile, _quelle(w), _aktion_daten(w.faehigkeit, arguments))
        return text

    async def daten(self, faehigkeit: str, arguments: dict, kanal: str = "app") -> Any:
        """Für die App (z. B. "Heute"): Werkzeug über die Policy aufrufen und JSON zurückgeben."""
        text = await self.aufrufen(faehigkeit, arguments, Kontext(kanal=kanal))
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    async def ausfuehren(self, w: Werkzeug, arguments: dict) -> str:
        """Führt ein Werkzeug ohne Prüfung aus. Nur nach Policy-Entscheid oder Handy-Freigabe aufrufen."""
        if w.connector == "kern":
            try:
                ergebnis = await self.kern.werkzeuge[w.faehigkeit].fn(**arguments)
            except TypeError as exc:
                return f"Fehler: falsche Argumente für {w.faehigkeit}: {exc}"
            except Exception as exc:  # noqa: BLE001
                log.exception("Kern-Werkzeug %s fehlgeschlagen", w.faehigkeit)
                return f"Fehler bei {w.faehigkeit}: {exc}"
            text = ergebnis if isinstance(ergebnis, str) else json.dumps(ergebnis, ensure_ascii=False, default=str)
            return text[:MAX_ERGEBNIS]
        try:
            async with Client(w.url) as client:
                result = await client.call_tool(w.funktion, arguments)
        except Exception as exc:  # noqa: BLE001
            log.exception("Werkzeug %s fehlgeschlagen", w.faehigkeit)
            self.erreichbar[w.connector] = False
            return f"Fehler beim Aufruf von {w.faehigkeit}: {exc}"
        teile = [c.text for c in result.content if getattr(c, "type", "") == "text"]
        if result.structured_content is not None and not result.is_error:
            sc = result.structured_content
            # FastMCP verpackt Listen und einfache Werte als {"result": ...}
            if isinstance(sc, dict) and set(sc) == {"result"}:
                sc = sc["result"]
            teile = [sc if isinstance(sc, str) else json.dumps(sc, ensure_ascii=False, default=str)]
        text = "\n".join(teile) or "(kein Ergebnis)"
        if result.is_error:
            text = f"Fehler: {text}"
        return text[:MAX_ERGEBNIS]

    async def _vorschau(self, w: Werkzeug, arguments: dict) -> str | None:
        """Was die Aktion konkret tut, für die Freigabe auf dem Handy (z. B. der Mail-Entwurf)."""
        m = self.manifeste.get(w.connector)
        tool = m.vorschau.get(w.faehigkeit) if m else None
        if not tool:
            return None
        try:
            async with Client(w.url) as client:
                result = await client.call_tool(tool, arguments)
            return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")[:3000] or None
        except Exception:  # noqa: BLE001
            log.exception("Vorschau für %s fehlgeschlagen", w.faehigkeit)
            return None

    async def nach_freigabe(self, row: dict) -> str:
        """Führt eine freigegebene Aktion aus und protokolliert sie."""
        args = row["arguments"] if isinstance(row["arguments"], dict) else json.loads(row["arguments"])
        w = self.finden(row["tool"])
        if w is None or (w.connector != "kern" and not self.erreichbar.get(w.connector)):
            await self.laden()
            w = self.finden(row["tool"])
        if w is None:
            return f"Fehler: Werkzeug {row['tool']} ist gerade nicht erreichbar."
        if grund := self.gesperrt(w):
            return f"Nicht ausgeführt: {grund}."
        text = await self.ausfuehren(w, args)
        ctx = Kontext(kanal=row["kanal"], belastet=row["belastet"])
        await self._audit(w.faehigkeit, w.connector, self.policy.stufe(w.faehigkeit).value,
                          "ausgefuehrt_nach_freigabe", ctx, args, text[:500])
        if zeile := _aktion_text(w.faehigkeit, args, text):
            await self._ereignis("aktion", f"{zeile} (nach Freigabe)", _quelle(w), _aktion_daten(w.faehigkeit, args))
        return text

    async def freigabe_protokoll(self, row: dict, entscheid: str, text: str = "") -> None:
        """Protokolliert Entscheidungen zu einer Freigabe (Handy oder App, auch falsche PINs)."""
        args = row["arguments"] if isinstance(row["arguments"], dict) else json.loads(row["arguments"])
        w = self.finden(row["tool"])
        await self._audit(row["tool"], w.connector if w else None, "freigabe", entscheid,
                          Kontext(kanal=row["kanal"], belastet=row["belastet"]), args, text[:500])

    # ------------------------------------------------------------------ Protokoll

    async def _audit(self, tool: str, connector: str | None, stufe: str, entscheid: str, ctx: Kontext,
                     arguments: dict, ergebnis: str) -> None:
        try:
            async with await self.store.conn() as conn:
                await conn.execute(
                    """INSERT INTO audit_log (tool, connector, stufe, entscheid, kanal, belastet, arguments, ergebnis)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (tool, connector, stufe, entscheid, ctx.kanal, ctx.belastet,
                     json.dumps(arguments, default=str), ergebnis),
                )
        except Exception:  # noqa: BLE001
            log.exception("audit_log konnte nicht geschrieben werden")

    async def _ereignis(self, art: str, text: str, quelle: str, daten: dict | None = None) -> None:
        try:
            await self.store.ereignis(art, text, quelle, daten)
        except Exception:  # noqa: BLE001
            log.exception("Ereignis konnte nicht geschrieben werden")


def _aktion_text(faehigkeit: str, arguments: dict, ergebnis: str) -> str | None:
    """Timeline-Zeile mit Inhalt statt Ergebnis-Rohtext. None: kein eigenes Ereignis nötig."""
    letzter = faehigkeit.rsplit(".", 1)[-1]
    if faehigkeit.endswith("calendar.create"):
        return None  # der Kalender schreibt selbst „Termin eingetragen: …“
    if faehigkeit == "memory.remember" and arguments.get("text"):
        return f"Gemerkt: {str(arguments['text'])[:200]}"
    if faehigkeit == "tasks.create" and arguments.get("titel"):
        return f"Aufgabe angelegt: {str(arguments['titel'])[:200]}"
    if letzter == "draft" and arguments.get("betreff"):
        return f"Mail-Entwurf: {str(arguments['betreff'])[:200]}"
    return f"{faehigkeit}: {_kurz(ergebnis)}"


def _aktion_daten(faehigkeit: str, arguments: dict) -> dict:
    d = {"werkzeug": faehigkeit}
    if faehigkeit == "memory.remember" and arguments.get("text"):
        d["inhalt"] = str(arguments["text"])[:500]
    return d


def _kurz(text: str) -> str:
    """Für die Timeline: Klartext kürzen, JSON-Ergebnisse nicht ausschütten."""
    if text.lstrip().startswith(("{", "[")):
        try:
            daten = json.loads(text)
            if isinstance(daten, dict):
                return ", ".join(f"{k}={v}" for k, v in list(daten.items())[:3] if not isinstance(v, (dict, list)))[:160]
            return f"{len(daten)} Einträge"
        except json.JSONDecodeError:
            pass
    return text[:160]


def _quelle(w: Werkzeug) -> str:
    return "core" if w.connector == "kern" else f"connector:{w.connector}"
