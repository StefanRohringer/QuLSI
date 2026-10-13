"""Einstellungen aus der App: alles, was man im Alltag umstellen will, ohne Dateien auf der Box.

- GET  /api/einstellungen                       alle Bereiche mit aktuellem Wert, Grundwert und Auswahl
- POST /api/einstellungen/{bereich}             {"schluessel": wert, ...}  (null = zurück auf den Grundwert)
- POST /api/einstellungen/aktion/{name}         benachrichtigung-testen | memchip-sichern | organisieren

Die Werte aus .env und config/*.yaml sind der Grundwert. Was in der App geändert wird, liegt in
Postgres (system_state "einstellungen"), wirkt sofort und übersteht Neustarts.

Sicherheit (Policy) lässt sich in der App nur strenger stellen, nie lockerer als in config/policy.yaml:
wer den App-Schlüssel stiehlt, soll damit keine Freigaben abschalten können. Lockerer geht nur in der
Datei auf der Box. Aus demselben Grund ist das ntfy-Thema (wohin die Freigabe-Knöpfe gehen) hier nur
lesbar, und Werkzeuge und Rechte der Hintergrund-Agents bleiben in config/agenten.yaml.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request

from .policy import Entscheid, Stufe
from .proaktiv.zeitplan import Ruhezeit
from .router import bilder_verfuegbar

# Lesbare Namen für die Auswahllisten in der App (die technischen Namen bleiben die Werte).
MODELL_TITEL = {
    "bonsai-fast": "Bonsai schnell",
    "bonsai-think": "Bonsai mit Denkmodus",
    "bonsai-vision": "Bonsai mit Bildern",
    "qulsi-klein": "Qwen3 1.7B (klein, schnell)",
    "cloud": "Cloud-Modell",
    "aus": "Aus",
}


def modell_titel(name: str | None) -> str:
    return MODELL_TITEL.get(name or "aus", name or "Aus")

log = logging.getLogger("qulsi.einstellungen")

router = APIRouter(prefix="/api/einstellungen")

STRENGE_STUFE = {Stufe.LOW: 0, Stufe.MEDIUM: 1, Stufe.HIGH: 2, Stufe.DENY: 3}
STRENGE_ENTSCHEID = {Entscheid.ERLAUBT: 0, Entscheid.BESTAETIGUNG: 1, Entscheid.ABGELEHNT: 2}
ENTSCHEID_TEXT = {Entscheid.ERLAUBT: "erlaubt", Entscheid.BESTAETIGUNG: "bestaetigung", Entscheid.ABGELEHNT: "verboten"}
TEXT_ENTSCHEID = {"erlaubt": Entscheid.ERLAUBT, "bestaetigung": Entscheid.BESTAETIGUNG, "verboten": Entscheid.ABGELEHNT}


def _version() -> dict:
    """Stand des Pakets: version.json neben diesem Modul (schreibt scripts/paket.sh), sonst QULSI_VERSION/QULSI_GEBAUT."""
    try:
        d = json.loads(Path(__file__).with_name("version.json").read_text())
        return {"version": d.get("version"), "gebaut": d.get("gebaut"), "neu": d.get("neu") or []}
    except (OSError, ValueError):
        return {"version": os.environ.get("QULSI_VERSION") or None, "gebaut": os.environ.get("QULSI_GEBAUT") or None}


def _zahl(wert: Any, von: float, bis: float, name: str, ganz: bool = True) -> float:
    try:
        z = int(wert) if ganz else float(wert)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: bitte eine Zahl") from None
    if not von <= z <= bis:
        raise ValueError(f"{name}: erlaubt sind {von:g} bis {bis:g}")
    return z


@dataclass
class Grundwerte:
    """Stand aus .env und config/*.yaml beim Start, damit "zurücksetzen" und "nur strenger" funktionieren."""

    offline: str
    modelle: dict[str, str | None]
    gross_ab: int
    klein_bis: int
    policy_overrides: dict[str, Stufe]
    belastet_medium: Entscheid
    belastet_high: Entscheid
    automatik_medium: Entscheid
    bestaetigung_sekunden: int
    limits: dict[Stufe, int]
    ruhezeit: Ruhezeit
    max_hinweise: int
    organisator_minuten: float
    memchip_stunden: float


@dataclass
class Einstellungen:
    state: Any  # app.state
    werte: dict[str, dict] = field(default_factory=dict)  # nur die in der App geänderten Werte
    grund: Grundwerte | None = None
    _organisator_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ Teile des Systems

    @property
    def hub(self):
        return self.state.hub

    @property
    def policy(self):
        return self.state.hub.policy

    @property
    def router(self):
        return self.state.agent.router

    @property
    def hintergrund(self):
        return getattr(self.state, "hintergrund", None)

    @property
    def kern(self):
        return self.state.hub.kern

    def grund_merken(self) -> None:
        p, r, hg, k = self.policy, self.router, self.hintergrund, self.kern
        self.grund = Grundwerte(
            offline=self.hub.offline_modus, modelle=dict(r.modelle), gross_ab=r.gross_ab, klein_bis=r.klein_bis,
            policy_overrides=dict(p.overrides), belastet_medium=p.belastet_medium, belastet_high=p.belastet_high,
            automatik_medium=p.automatik_medium, bestaetigung_sekunden=p.gueltig_sekunden,
            limits=dict(p.limits_stufe),
            ruhezeit=hg.einst.ruhezeit if hg else Ruhezeit(),
            max_hinweise=hg.einst.max_hinweise_pro_stunde if hg else 6,
            organisator_minuten=k.organisator.minuten, memchip_stunden=k.memchip.auto_stunden,
        )

    # ------------------------------------------------------------------ Laden und Speichern

    async def laden(self) -> None:
        """Beim Start nach dem Aufbau von Hub, Router und Hintergrund-Agents aufrufen."""
        self.grund_merken()
        row = await self.state.store.eine("SELECT value FROM system_state WHERE key = 'einstellungen'")
        gespeichert = (row or {}).get("value") or {}
        if isinstance(gespeichert, str):
            gespeichert = json.loads(gespeichert)
        for bereich, werte in gespeichert.items():
            try:
                self.anwenden(bereich, werte)
            except (ValueError, KeyError) as exc:  # z. B. policy.yaml wurde inzwischen strenger
                log.warning("Einstellung %s nicht übernommen: %s", bereich, exc)

    async def speichern(self) -> None:
        async with await self.state.store.conn() as conn:
            await conn.execute(
                """INSERT INTO system_state (key, value) VALUES ('einstellungen', %s::jsonb)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, zeit = now()""",
                (json.dumps(self.werte),))

    async def setzen(self, bereich: str, aenderung: dict) -> None:
        neu = {**self.werte.get(bereich, {}), **aenderung}
        neu = {k: v for k, v in neu.items() if v is not None}
        if bereich == "sicherheit" and "stufen" in aenderung:
            stufen = {**self.werte.get(bereich, {}).get("stufen", {}), **(aenderung["stufen"] or {})}
            neu["stufen"] = {k: v for k, v in stufen.items() if v}
        self.anwenden(bereich, neu, voll=True)
        await self.speichern()
        if bereich == "stimme" and "stimme" in aenderung:
            self.stimme_vorladen(str(aenderung["stimme"] or ""))

    # ------------------------------------------------------------------ Anwenden

    def anwenden(self, bereich: str, werte: dict, voll: bool = False) -> None:
        """Prüft und setzt einen Bereich. Bei Fehlern bleibt alles, wie es war (ValueError)."""
        fn = getattr(self, f"_bereich_{bereich}", None)
        if fn is None:
            raise ValueError(f"Bereich {bereich} gibt es nicht")
        unbekannt = set(werte) - set(BEREICHE[bereich])
        if unbekannt:
            raise ValueError(f"Unbekannte Einstellung: {', '.join(sorted(unbekannt))}")
        fn(werte)
        if werte:
            self.werte[bereich] = werte
        else:
            self.werte.pop(bereich, None)

    def _bereich_system(self, w: dict) -> None:
        offline = str(w.get("offline", self.grund.offline)).lower()
        if offline not in ("auto", "true", "false"):
            raise ValueError("offline: auto, true oder false")
        self.hub.offline_modus = offline
        self.hub._offline_cache = (0.0, self.hub._offline_cache[1])

    def _bereich_modelle(self, w: dict) -> None:
        g = self.grund
        modelle = dict(g.modelle)
        for rolle in ("klein", "standard", "gross", "vision"):
            if rolle in w:
                v = str(w[rolle]).strip()
                if not v:
                    raise ValueError(f"{rolle}: Modellname fehlt")
                if v == "aus" and rolle == "standard":
                    raise ValueError("Das Standardmodell kann nicht aus sein")
                modelle[rolle] = None if v == "aus" else v
        gross_ab = _zahl(w.get("gross_ab_zeichen", g.gross_ab), 100, 10000, "Großes Modell ab Zeichen")
        klein_bis = _zahl(w.get("klein_bis_zeichen", g.klein_bis), 0, 1000, "Kleines Modell bis Zeichen")
        r = self.router
        r.modelle.clear()
        r.modelle.update(modelle)
        r.gross_ab, r.klein_bis = gross_ab, klein_bis

    def _bereich_sicherheit(self, w: dict) -> None:
        g, p = self.grund, self.policy

        def entscheid(schluessel: str, basis: Entscheid, erlaubt: tuple[str, ...]) -> Entscheid:
            if schluessel not in w:
                return basis
            text = str(w[schluessel])
            if text not in erlaubt:
                raise ValueError(f"{schluessel}: {' | '.join(erlaubt)}")
            e = TEXT_ENTSCHEID[text]
            if STRENGE_ENTSCHEID[e] < STRENGE_ENTSCHEID[basis]:
                raise ValueError(f"{schluessel}: in der App nur strenger als in policy.yaml ({ENTSCHEID_TEXT[basis]})")
            return e

        bm = entscheid("belastet_medium", g.belastet_medium, ("erlaubt", "bestaetigung", "verboten"))
        bh = entscheid("belastet_high", g.belastet_high, ("bestaetigung", "verboten"))
        am = entscheid("automatik_medium", g.automatik_medium, ("bestaetigung", "verboten"))
        sek = _zahl(w.get("bestaetigung_sekunden", g.bestaetigung_sekunden), 30, g.bestaetigung_sekunden,
                    "Freigabe gültig (Sekunden)")
        limits = dict(g.limits)
        for st in (Stufe.LOW, Stufe.MEDIUM, Stufe.HIGH):
            k = f"limit_{st.value}"
            if k in w:
                obergrenze = g.limits.get(st) or 100000
                limits[st] = int(_zahl(w[k], 1, obergrenze, f"Limit {st.value} pro Stunde"))
        overrides = dict(g.policy_overrides)
        for faehigkeit, wert in (w.get("stufen") or {}).items():
            try:
                neu = Stufe(wert)
            except ValueError:
                raise ValueError(f"{faehigkeit}: low, medium, high oder deny") from None
            basis = g.policy_overrides.get(faehigkeit) or p.stufen.get(faehigkeit) or Stufe.HIGH
            if STRENGE_STUFE[neu] < STRENGE_STUFE[basis]:
                raise ValueError(f"{faehigkeit}: in der App nur strenger als {basis.value}")
            overrides[faehigkeit] = neu
        p.belastet_medium, p.belastet_high, p.automatik_medium = bm, bh, am
        p.gueltig_sekunden = int(sek)
        self.state.freigaben.gueltig = int(sek)
        p.limits_stufe.clear()
        p.limits_stufe.update(limits)
        p.overrides.clear()
        p.overrides.update(overrides)

    def _bereich_agenten(self, w: dict) -> None:
        hg, g = self.hintergrund, self.grund
        if hg is None:
            raise ValueError("Hintergrund-Agents laufen nicht")
        ruhe = Ruhezeit.lesen(w["ruhezeit"]) if "ruhezeit" in w else g.ruhezeit
        maxh = _zahl(w.get("max_hinweise_pro_stunde", g.max_hinweise), 0, 60, "Hinweise pro Stunde")
        hg.einst.ruhezeit, hg.einst.max_hinweise_pro_stunde = ruhe, int(maxh)

    def _bereich_gedaechtnis(self, w: dict) -> None:
        g, k = self.grund, self.kern
        minuten = _zahl(w.get("organisator_minuten", g.organisator_minuten), 0, 1440, "Organisator (Minuten)", False)
        stunden = _zahl(w.get("memchip_stunden", g.memchip_stunden), 0, 720, "Memchip-Sicherung (Stunden)", False)
        war_aus = k.organisator.minuten <= 0
        k.organisator.minuten, k.memchip.auto_stunden = minuten, stunden
        if war_aus and minuten > 0 and self._organisator_laeuft() is False:
            try:
                self._organisator_task = asyncio.get_running_loop().create_task(k.organisator.schleife())
            except RuntimeError:  # kein Event-Loop (Tests): startet beim nächsten Neustart
                pass

    def _organisator_laeuft(self) -> bool:
        t = getattr(self.state, "organisator_task", None) or self._organisator_task
        return bool(t and not t.done())

    def _bereich_stimme(self, w: dict) -> None:
        if "stimme" in w and not re.fullmatch(r"[a-z0-9_]{0,40}", str(w["stimme"])):
            raise ValueError("stimme: eigene oder ein Sprecher aus der Liste")
        if "vorlesen" in w and not isinstance(w["vorlesen"], bool):
            raise ValueError("vorlesen: true oder false")

    # ------------------------------------------------------------------ Ansicht für die App

    async def _modell_liste(self) -> list[str]:
        s = self.state.settings
        try:
            async with httpx.AsyncClient(timeout=4, trust_env=False) as c:
                r = await c.get(f"{s.llm_base_url}/models", headers={"Authorization": f"Bearer {s.llm_api_key}"})
                return sorted(m["id"] for m in r.json().get("data", []) if m.get("id") != "embed")
        except Exception:  # noqa: BLE001
            return []

    async def _stimme_status(self) -> dict:
        s = self.state.settings
        try:
            async with httpx.AsyncClient(timeout=4, trust_env=False) as c:
                status = (await c.get(f"{s.voice_url}/health")).json()
                # welche Stimmen die TTS-Dienste anbieten (ohne ein Modell zu laden); Chatterbox kommt hinten dran
                for url in (s.tts_url, getattr(s, "tts_chatterbox_url", "")):
                    if not url:
                        continue
                    try:
                        neu = (await c.get(f"{url}/health")).json().get("stimmen") or {}
                    except Exception:  # noqa: BLE001
                        continue
                    if "stimmen" not in status:
                        status["stimmen"] = neu
                    else:
                        status["stimmen"]["sprecher"] = [*status["stimmen"].get("sprecher", []),
                                                         *neu.get("sprecher", [])]
                return status
        except Exception:  # noqa: BLE001
            return {"ok": False}

    def stimme_vorladen(self, stimme: str) -> None:
        """Nach einem Wechsel lädt der TTS-Dienst die gewählte Stimme gleich, nicht erst beim ersten Satz."""
        s = self.state.settings
        url = getattr(s, "tts_chatterbox_url", "") if stimme.startswith("chatterbox") else s.tts_url
        if not url:
            return

        async def los():
            try:
                async with httpx.AsyncClient(timeout=5, trust_env=False) as c:
                    await c.post(f"{url}/vorwaermen", json={"stimme": stimme or None})
            except Exception:  # noqa: BLE001  dann lädt sie eben beim ersten Satz
                pass
        aufgabe = asyncio.create_task(los())
        self._aufgaben = getattr(self, "_aufgaben", set())
        self._aufgaben.add(aufgabe)
        aufgabe.add_done_callback(self._aufgaben.discard)

    async def ansicht(self) -> dict:
        g, p, r, hub, k = self.grund, self.policy, self.router, self.hub, self.kern
        s = self.state.settings
        hg = self.hintergrund
        geaendert = {b: sorted(w) for b, w in self.werte.items()}
        werkzeuge = [{"faehigkeit": f, "connector": wz.connector,
                      "grund": (g.policy_overrides.get(f) or p.stufen.get(f) or Stufe.HIGH).value,
                      "stufe": p.stufe(f).value}
                     for f, wz in sorted(hub.werkzeuge.items())]
        ruhe = hg.einst.ruhezeit if hg else Ruhezeit()
        modelle, stimme, memchip = await asyncio.gather(self._modell_liste(), self._stimme_status(), k.memchip.status())
        return {
            "geaendert": geaendert,
            "system": {
                "offline": hub.offline_modus, "offline_grund": g.offline, "jetzt_offline": await hub.offline(),
                "public_url": s.public_url, "zeitzone": s.timezone, **_version(),
            },
            "modelle": {
                "rollen": {k2: v or "aus" for k2, v in r.modelle.items()},
                "grund": {k2: v or "aus" for k2, v in g.modelle.items()},
                "verfuegbar": modelle,
                "titel": {m: modell_titel(m) for m in [*modelle, *r.modelle.values(), "aus"] if m},
                # Bilder: ohne Bild-Projektor (mmproj) kann kein Modell Bilder sehen.
                "bilder": {"verfuegbar": bilder_verfuegbar(),
                           "hinweis": None if bilder_verfuegbar() else "nicht verfügbar"},
                "gross_ab_zeichen": r.gross_ab, "klein_bis_zeichen": r.klein_bis,
            },
            "sicherheit": {
                "belastet_medium": ENTSCHEID_TEXT[p.belastet_medium], "belastet_high": ENTSCHEID_TEXT[p.belastet_high],
                "automatik_medium": ENTSCHEID_TEXT[p.automatik_medium],
                "bestaetigung_sekunden": p.gueltig_sekunden,
                "limits": {st.value: p.limits_stufe.get(st) for st in (Stufe.LOW, Stufe.MEDIUM, Stufe.HIGH)},
                "grund": {"belastet_medium": ENTSCHEID_TEXT[g.belastet_medium],
                          "belastet_high": ENTSCHEID_TEXT[g.belastet_high],
                          "automatik_medium": ENTSCHEID_TEXT[g.automatik_medium],
                          "bestaetigung_sekunden": g.bestaetigung_sekunden,
                          "limits": {st.value: g.limits.get(st) for st in (Stufe.LOW, Stufe.MEDIUM, Stufe.HIGH)}},
                "werkzeuge": werkzeuge,
            },
            "agenten": {
                "an": bool(hg and hg.an),
                "ruhezeit": f"{ruhe.von:%H:%M}-{ruhe.bis:%H:%M}" if ruhe.von is not None and ruhe.bis is not None else "",
                "max_hinweise_pro_stunde": hg.einst.max_hinweise_pro_stunde if hg else None,
            },
            "benachrichtigungen": {
                "eingerichtet": bool(s.ntfy_url), "server": s.ntfy_url or None, "thema": s.ntfy_topic,
                "token": bool(s.ntfy_token),
                # Adresse, unter der das Handy ntfy erreicht (für QR-Code und Abo-Link in der App).
                "oeffentlich": (getattr(s, "ntfy_public_url", "") or os.environ.get("NTFY_PUBLIC_URL", "")).rstrip("/") or None,
            },
            "gedaechtnis": {
                "organisator_minuten": k.organisator.minuten, "memchip_stunden": k.memchip.auto_stunden,
                "memchip": memchip, "organisator_letzter_lauf": k.organisator.letzter_lauf or None,
            },
            "stimme": {
                "stt": stimme.get("stt"), "tts": stimme.get("tts"), "erreichbar": bool(stimme.get("ok")),
                "modell": stimme.get("tts_modell"), "ersatz": stimme.get("ersatz"),
                "stimme": self.werte.get("stimme", {}).get("stimme", ""),
                "vorlesen": self.werte.get("stimme", {}).get("vorlesen", True),
                **stimm_auswahl(stimme.get("stimmen"), self.werte.get("stimme", {}).get("stimme", "")),
            },
        }


# Eingebaute Sprecher: Anzeigename und Art. Die Qwen3-Sprecher sprechen Deutsch mit leichtem Akzent ihrer Sprache.
SPRECHER_INFO = {
    "aiden": ("Aiden", "männlich, Englisch"), "ryan": ("Ryan", "männlich, Englisch"),
    "dylan": ("Dylan", "männlich, Chinesisch"), "eric": ("Eric", "männlich, Chinesisch"),
    "uncle_fu": ("Uncle Fu", "männlich, Chinesisch"), "vivian": ("Vivian", "weiblich, Chinesisch"),
    "serena": ("Serena", "weiblich, Chinesisch"), "ono_anna": ("Ono Anna", "weiblich, Japanisch"),
    "sohee": ("Sohee", "weiblich, Koreanisch"),
    "eigene": ("Eigene Stimme", "aus deiner Stimmprobe"),
    "chatterbox": ("Chatterbox", "natürlicheres Deutsch, eingebaute Stimme"),
    "chatterbox_eigene": ("Eigene Stimme mit Chatterbox", "aus deiner Stimmprobe"),
}


def stimm_auswahl(stimmen: dict | None, gewaehlt: str) -> dict:
    """Auswahlliste für die App: eigene Stimme (wenn es eine Probe gibt) und die eingebauten Sprecher.
    Leer gewählt = automatisch: die eigene Stimme, wenn es sie gibt, sonst der Standard-Sprecher."""
    if not stimmen:
        return {"auswahl": [], "aktiv": gewaehlt or None, "eigene_vorhanden": False}
    eigene = bool(stimmen.get("eigene"))
    sprecher = [str(x) for x in stimmen.get("sprecher") or []]
    auswahl = [{"id": "eigene", "name": "Eigene Stimme", "hinweis": "aus deiner Stimmprobe"}] if eigene else []
    for x in sprecher:
        gross = x.endswith("_17b")  # Qwen3-TTS 1.7B: besserer Klang, langsamer
        basis = x[:-4] if gross else x
        name, art = SPRECHER_INFO.get(basis, (basis.replace("_", " ").title(), ""))
        if gross:
            name, art = f"{name} (hochwertig)", "Qwen3 1.7B, besserer Klang, braucht etwas länger"
        auswahl.append({"id": x, "name": name, "hinweis": art})
    ids = {a["id"] for a in auswahl}
    aktiv = gewaehlt if gewaehlt in ids else ("eigene" if eigene else stimmen.get("standard") or None)
    if aktiv == "eigene" and not eigene:
        aktiv = stimmen.get("standard")
    return {"auswahl": auswahl, "aktiv": aktiv, "eigene_vorhanden": eigene}


BEREICHE: dict[str, tuple[str, ...]] = {
    "system": ("offline",),
    "modelle": ("klein", "standard", "gross", "vision", "gross_ab_zeichen", "klein_bis_zeichen"),
    "sicherheit": ("belastet_medium", "belastet_high", "automatik_medium", "bestaetigung_sekunden",
                   "limit_low", "limit_medium", "limit_high", "stufen"),
    "agenten": ("ruhezeit", "max_hinweise_pro_stunde"),
    "gedaechtnis": ("organisator_minuten", "memchip_stunden"),
    "stimme": ("stimme", "vorlesen"),
}


# --------------------------------------------------------------------------- HTTP


def _e(request: Request) -> Einstellungen:
    e = getattr(request.app.state, "einstellungen", None)
    if e is None:
        raise HTTPException(503, "Einstellungen noch nicht bereit")
    return e


@router.get("")
async def alle(request: Request):
    return await _e(request).ansicht()


@router.post("/aktion/{name}")
async def aktion(request: Request, name: str):
    e = _e(request)
    if name == "benachrichtigung-testen":
        await e.state.notifier.senden("QuLSI", "Test aus der App: Benachrichtigungen kommen an.")
        return {"ok": True, "eingerichtet": bool(e.state.settings.ntfy_url)}
    if name == "memchip-sichern":
        try:
            return {"datei": await e.kern.memchip.exportieren()}
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
    if name == "organisieren":
        return await e.kern.organisator.lauf()
    raise HTTPException(404, f"Aktion {name} gibt es nicht")


@router.post("/{bereich}")
async def setzen(request: Request, bereich: str):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Bitte ein Objekt schicken")
    e = _e(request)
    if bereich not in BEREICHE:
        raise HTTPException(404, f"Bereich {bereich} gibt es nicht")
    try:
        await e.setzen(bereich, body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}
