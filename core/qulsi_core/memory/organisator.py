"""Organisator: räumt im Hintergrund das Gedächtnis auf (läuft im Core, alle paar Minuten).

Quellen -> Erinnerungen:
  Termine       vergangene Termine werden episodisch, mit Ort und erkannten Personen
  Dateien       neu indizierte Dateien (Dateien-Connector) werden ein Eintrag in der Timeline
  Mails         Absender und Betreff der letzten Tage (über den Mail-Connector und die Policy)
  Fotos         FOTOS_DIR: Aufnahmezeit und GPS aus EXIF, Ort aus bekannten Orten, Album = Ordner
Danach:
  Fototage      "23 Fotos in Gardasee" pro Tag und Ort
  Kontakte      wer oft schreibt, wird als Person angelegt und verknüpft
  Gewohnheiten  wiederkehrende Termine und Aktionen werden Vorlieben (praeferenz)
  Rückblick     ein Eintrag pro Tag: was war los
  Nachholen     Erinnerungen ohne Vektor einbetten

Alles ist idempotent (quelle_ref), ein Lauf kann jederzeit abbrechen und neu starten.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import date, datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from ..feiertage import feiertage_jahr
from .fotos import ENDUNGEN as FOTO_ENDUNGEN
from .fotos import exif_lesen, vorschau
from .gedaechtnis import Gedaechtnis
from .objects import ObjectStore
from .zeitraum import WOCHENTAG_NAMEN, ort_normalisieren

log = logging.getLogger("organisator")
KEINE_PERSON = ("noreply", "no-reply", "newsletter", "mailer-daemon", "notification", "benachrichtigung", "info@", "news@")


def _h(*teile: Any) -> str:
    return hashlib.sha256("|".join(str(t) for t in teile).encode()).hexdigest()[:16]


class Organisator:
    def __init__(self, g: Gedaechtnis, hub=None, objekte: ObjectStore | None = None, fotos_dir: str | None = None,
                 minuten: float | None = None, memchip=None):
        self.g = g
        self.store = g.store
        self.hub = hub
        self.objekte = objekte or ObjectStore()
        self.fotos_dir = Path(fotos_dir or os.environ.get("FOTOS_DIR", "/data/fotos"))
        self.minuten = minuten if minuten is not None else float(os.environ.get("ORGANISATOR_MINUTEN", "10"))
        self.fotos_pro_lauf = int(os.environ.get("FOTOS_PRO_LAUF", "300"))
        self.memchip = memchip
        self.letzter_lauf: dict = {}
        self._sperre = asyncio.Lock()

    async def schleife(self) -> None:
        if self.minuten <= 0:
            log.info("Organisator ist aus (ORGANISATOR_MINUTEN=0)")
            return
        await asyncio.sleep(float(os.environ.get("ORGANISATOR_START_SEKUNDEN", "20")))
        while True:
            try:
                await self.lauf()
            except Exception:  # noqa: BLE001
                log.exception("Organisator-Lauf fehlgeschlagen")
            await asyncio.sleep(self.minuten * 60)

    async def lauf(self) -> dict:
        """Ein Durchgang über alle Quellen. Gibt zurück, wie viel neu dazukam."""
        await self.g.bereit()
        async with self._sperre:
            ergebnis: dict[str, int | str] = {}
            for name, schritt in (("termine", self.termine), ("dateien", self.dateien), ("mails", self.mails),
                                  ("fotos", self.fotos), ("fototage", self.fototage), ("kontakte", self.kontakte),
                                  ("gewohnheiten", self.gewohnheiten), ("rueckblick", self.rueckblick),
                                  ("eingebettet", self.g.nachholen)):
                try:
                    ergebnis[name] = await schritt()
                except Exception as exc:  # noqa: BLE001  eine kaputte Quelle hält die anderen nicht auf
                    log.warning("Organisator %s: %s", name, exc)
                    ergebnis[name] = f"Fehler: {exc}"
            if self.memchip:
                try:
                    if datei := await self.memchip.automatisch():
                        ergebnis["memchip"] = datei
                except Exception as exc:  # noqa: BLE001
                    log.warning("Memchip-Sicherung: %s", exc)
            neu = sum(v for k, v in ergebnis.items() if isinstance(v, int) and k != "eingebettet")
            if neu:
                await self.store.ereignis("organisiert", f"Gedächtnis organisiert: {neu} neue Einträge", "core:gedaechtnis", ergebnis)
            self.letzter_lauf = {"zeit": datetime.now(timezone.utc).isoformat(), **ergebnis}
            return ergebnis

    # ------------------------------------------------------------------ Termine

    async def termine(self) -> int:
        rows = await self.store.alle(
            """SELECT quelle, uid, titel, start, ende, ganztags, ort, beschreibung, extern FROM termine
               WHERE start <= now() AND start > now() - interval '400 days' ORDER BY start""")
        # Feiertage, die frühere Versionen als Erinnerung gespeichert haben, wieder entfernen
        alt = await self.store.alle(
            """SELECT id, quelle_ref, daten FROM erinnerungen WHERE quelle = 'kalender' AND quelle_ref LIKE 'termin:%%'
               AND coalesce((daten->>'ganztags')::boolean, false)""")
        weg = [r["id"] for r in alt if _feiertag({"ganztags": True, "titel": (r["daten"] or {}).get("titel"),
                                                  "quelle": ":".join((r["quelle_ref"] or "").split(":")[1:3])})]
        for i in weg:
            await self.g.vergessen(i)
        neu = 0
        for t in rows:
            if _feiertag(t):  # Feiertage sind kein Erlebnis; die rechnet holidays.list bei Bedarf aus
                continue
            ort = ort_normalisieren(t["ort"] or "")
            text = f"Termin: {t['titel']}" + (f" in {ort}" if ort else "")
            if t["beschreibung"]:
                text += f". {' '.join(t['beschreibung'].split())[:200]}"
            # Fremd nur, wenn Titel/Beschreibung von Dritten stammen (Einladung); eigene Termine nie
            extern = bool(t.get("extern")) and t["quelle"] != "lokal"
            if await self.g.speichern(text, "episodisch", t["start"], ort=ort or None, quelle="kalender",
                                      quelle_ref=f"termin:{t['quelle']}:{t['uid']}:{t['start'].isoformat()}",
                                      daten={"titel": t["titel"], "ganztags": t["ganztags"], "extern": extern},
                                      fremd=extern):
                neu += 1
        return neu

    # ------------------------------------------------------------------ Dateien

    async def _auszug(self, quelle: str) -> str:
        try:
            from qdrant_client import models

            punkte, _ = await self.g.vek.client.scroll("dokumente", limit=1, with_payload=True, scroll_filter=models.Filter(must=[
                models.FieldCondition(key="quelle", match=models.MatchValue(value=quelle)),
                models.FieldCondition(key="abschnitt", match=models.MatchValue(value=0))]))
            return (punkte[0].payload or {}).get("text", "")[:300] if punkte else ""
        except Exception:  # noqa: BLE001
            return ""

    async def dateien(self) -> int:
        rows = await self.store.alle(
            "SELECT quelle, titel, objekt_key, geaendert, indiziert FROM dokumente WHERE geaendert IS NOT NULL ORDER BY geaendert")
        neu = 0
        for d in rows:
            ref = f"datei:{d['quelle']}:{d['geaendert'].isoformat()}"
            if await self.store.eine("SELECT 1 AS x FROM erinnerungen WHERE quelle_ref = %s", (ref,)):
                continue
            ordner = str(Path(d["quelle"].removeprefix("datei:")).parent)
            text = f"Datei {d['titel']}" + (f" im Ordner {ordner}" if ordner not in (".", "") else "")
            if auszug := await self._auszug(d["quelle"]):
                text += f": {auszug}"
            if await self.g.speichern(text, "episodisch", d["geaendert"], quelle="datei", quelle_ref=ref,
                                      objekt_key=d["objekt_key"], daten={"datei": d["quelle"]}):
                neu += 1
        return neu

    # ------------------------------------------------------------------ Mails

    async def mails(self) -> int:
        if not self.hub or not self.hub.finden("email.search"):
            return 0
        liste = await self.hub.daten("email.search", {"suche": "", "tage": 3, "anzahl": 50}, kanal="organisator")
        if not isinstance(liste, list):
            return 0
        neu = 0
        for m in liste:
            name, adresse = parseaddr(m.get("von") or "")
            try:
                zeit = datetime.strptime(m.get("datum") or "", "%d.%m.%Y %H:%M").replace(tzinfo=self.g.tz)
            except ValueError:
                zeit = datetime.now(timezone.utc)
            p = await self.g.person_per_mail(adresse) if adresse else None
            text = f"Mail von {name or adresse}: {m.get('betreff') or '(ohne Betreff)'}"
            if m.get("auszug"):
                text += f". {' '.join(m['auszug'].split())[:200]}"
            if await self.g.speichern(text, "episodisch", zeit, quelle="mail",
                                      quelle_ref=f"mail:{_h(adresse, m.get('datum'), m.get('betreff'))}",
                                      person_ids=[p["id"]] if p else None,
                                      daten={"absender": name, "absender_mail": adresse.lower()}):
                neu += 1
        return neu

    async def kontakte(self, mindestens: int = 3) -> int:
        """Wer mindestens 3-mal geschrieben hat und einen Namen mitschickt, wird Person."""
        rows = await self.store.alle(
            """SELECT daten->>'absender_mail' AS mail, max(daten->>'absender') AS name, array_agg(id) AS ids
               FROM erinnerungen WHERE quelle = 'mail' AND person_id IS NULL AND coalesce(daten->>'absender', '') <> ''
               GROUP BY 1 HAVING count(*) >= %s""", (mindestens,))
        neu = 0
        for r in rows:
            if not r["mail"] or any(k in r["mail"] for k in KEINE_PERSON):
                continue
            name = " ".join(r["name"].replace('"', "").split())[:60]
            if not name or "@" in name:
                continue
            p = await self.g.person_per_mail(r["mail"])
            if not p:
                pid = await self.g.person_id(name)
                await self.store.eine(
                    """UPDATE personen SET kontakt = kontakt || jsonb_build_object('mail', %s::text),
                       notizen = coalesce(notizen, 'Automatisch aus Mails angelegt') WHERE id = %s RETURNING 1""",
                    (r["mail"], pid))
                neu += 1
            else:
                pid = p["id"]
            async with await self.store.conn() as c:
                await c.execute("UPDATE erinnerungen SET person_id = %s, eingebettet = false WHERE id = ANY(%s)", (pid, r["ids"]))
                for eid in r["ids"]:
                    await c.execute("INSERT INTO erinnerung_personen VALUES (%s, %s) ON CONFLICT DO NOTHING", (eid, pid))
        return neu

    # ------------------------------------------------------------------ Fotos

    async def fotos(self) -> int:
        if not self.fotos_dir.is_dir():
            return 0
        bekannt = {r["quelle"] for r in await self.store.alle("SELECT quelle FROM fotos")}
        neu = 0
        for pfad in sorted(self.fotos_dir.rglob("*")):
            if neu >= self.fotos_pro_lauf:
                break  # Rest im nächsten Lauf, damit die Box nicht minutenlang beschäftigt ist
            if not pfad.is_file() or pfad.suffix.lower() not in FOTO_ENDUNGEN or pfad.name.startswith("."):
                continue
            rel = str(pfad.relative_to(self.fotos_dir))
            quelle = f"foto:{rel}"
            if quelle in bekannt:
                continue
            try:
                daten = await asyncio.to_thread(pfad.read_bytes)
                await self._foto(daten, rel, datetime.fromtimestamp(pfad.stat().st_mtime, timezone.utc))
            except Exception as exc:  # noqa: BLE001  defekt oder HEIC ohne pillow-heif
                log.info("Foto %s übersprungen: %s", rel, exc)
                continue
            neu += 1
        return neu

    async def foto_einordnen(self, daten: bytes, rel: str) -> tuple[str, int | None]:
        """Ein hochgeladenes Foto sofort einordnen (rel relativ zu FOTOS_DIR). Gibt (Vorschau-Schlüssel, Erinnerung)."""
        await self.g.bereit()
        return await self._foto(daten, rel, datetime.now(timezone.utc))

    async def _foto(self, daten: bytes, rel: str, geaendert: datetime) -> tuple[str, int | None]:
        quelle = f"foto:{rel}"
        info = await asyncio.to_thread(exif_lesen, daten, self.g.tz)
        klein = await asyncio.to_thread(vorschau, daten)
        key = await asyncio.to_thread(self.objekte.ablegen, klein, ".jpg")
        if vorhanden := await self.store.eine("SELECT erinnerung_id FROM fotos WHERE quelle = %s", (quelle,)):
            return key, vorhanden["erinnerung_id"]
        zeit = info["aufgenommen"] or geaendert
        ort = await self.g.ort_bei(info["lat"], info["lon"]) if info["lat"] is not None else None
        album = str(Path(rel).parent)
        text = "Foto"
        if ort:
            text += f" in {ort['name']}"
        elif info["lat"] is not None:
            text += f" bei {info['lat']:.4f}, {info['lon']:.4f}"
        text += f" am {zeit.astimezone(self.g.tz).strftime('%d.%m.%Y %H:%M')}"
        if album not in (".", ""):
            text += f", Album {album.replace('/', ' / ')}"
        eid = await self.g.speichern(text, "episodisch", zeit, ort=ort and ort["id"], quelle="foto",
                                     quelle_ref=quelle, objekt_key=key,
                                     daten={"lat": info["lat"], "lon": info["lon"], "kamera": info["kamera"], "datei": rel})
        await self.store.eine(
            """INSERT INTO fotos (quelle, objekt_key, aufgenommen, lat, lon, ort_id, kamera, breite, hoehe, erinnerung_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (quelle) DO NOTHING RETURNING id""",
            (quelle, key, zeit, info["lat"], info["lon"], ort and ort["id"], info["kamera"], info["breite"],
             info["hoehe"], eid))
        return key, eid

    async def fototage(self, mindestens: int = 5) -> int:
        """Pro Tag und Ort ein Eintrag "12 Fotos in München" (wie ein Album-Moment)."""
        rows = await self.store.alle(
            """SELECT (f.aufgenommen AT TIME ZONE %s)::date AS tag, o.name AS ort, f.ort_id, count(*) AS anzahl,
                      min(f.aufgenommen) AS erstes, (array_agg(f.objekt_key ORDER BY f.aufgenommen))[1] AS titelbild
               FROM fotos f LEFT JOIN orte o ON o.id = f.ort_id
               WHERE f.aufgenommen < date_trunc('day', now()) GROUP BY 1, 2, 3 HAVING count(*) >= %s""",
            (str(self.g.tz), mindestens))
        neu = 0
        for r in rows:
            text = f"{r['anzahl']} Fotos" + (f" in {r['ort']}" if r["ort"] else "") + f" am {r['tag'].strftime('%d.%m.%Y')}"
            if await self.g.speichern(text, "episodisch", r["erstes"], ort=r["ort_id"], quelle="foto",
                                      quelle_ref=f"fototag:{r['tag']}:{r['ort_id']}", objekt_key=r["titelbild"],
                                      daten={"anzahl": r["anzahl"]}):
                neu += 1
        return neu

    # ------------------------------------------------------------------ Gewohnheiten

    async def gewohnheiten(self) -> int:
        neu = 0
        # Termine, die in mindestens 3 verschiedenen Wochen am gleichen Wochentag zur gleichen Stunde liegen
        for r in await self.store.alle(
                """SELECT max(titel) AS titel, extract(isodow FROM start AT TIME ZONE %(tz)s)::int AS wt,
                          extract(hour FROM start AT TIME ZONE %(tz)s)::int AS stunde,
                          count(DISTINCT date_trunc('week', start)) AS wochen
                   FROM termine WHERE NOT ganztags AND start > now() - interval '90 days' AND start <= now()
                   GROUP BY lower(titel), 2, 3 HAVING count(DISTINCT date_trunc('week', start)) >= 3""",
                {"tz": str(self.g.tz)}):
            text = f"Gewohnheit: {r['titel']} meist {WOCHENTAG_NAMEN[r['wt'] - 1]}s um {r['stunde']}:00 Uhr"
            if await self.g.speichern(text, "praeferenz", quelle="kalender",
                                      quelle_ref=f"gewohnheit:termin:{_h(r['titel'].lower(), r['wt'], r['stunde'])}",
                                      daten={"gewohnheit": True, "wochen": r["wochen"]}):
                neu += 1
        # Aktionen (Licht, Heizung, ...), die an mindestens 4 Tagen zur gleichen Stunde liefen
        for r in await self.store.alle(
                """SELECT tool, arguments, extract(hour FROM zeit AT TIME ZONE %(tz)s)::int AS stunde,
                          count(DISTINCT (zeit AT TIME ZONE %(tz)s)::date) AS tage
                   FROM audit_log WHERE zeit > now() - interval '30 days' AND stufe <> 'low'
                     AND entscheid IN ('erlaubt', 'ausgefuehrt_nach_freigabe')
                     AND tool NOT LIKE 'memory.%%' AND tool NOT LIKE 'memchip.%%'
                   GROUP BY 1, 2, 3 HAVING count(DISTINCT (zeit AT TIME ZONE %(tz)s)::date) >= 4""",
                {"tz": str(self.g.tz)}):
            args = ", ".join(f"{k}={v}" for k, v in (r["arguments"] or {}).items())[:120]
            text = f"Gewohnheit: {r['tool']}" + (f" ({args})" if args else "") + f" meist gegen {r['stunde']}:00 Uhr"
            if await self.g.speichern(text, "praeferenz", quelle="gewohnheit", fremd=False,
                                      quelle_ref=f"gewohnheit:aktion:{_h(r['tool'], args, r['stunde'])}",
                                      daten={"gewohnheit": True, "tage": r["tage"], "tool": r["tool"]}):
                neu += 1
        return neu

    # ------------------------------------------------------------------ Tagesrückblick

    async def rueckblick(self, tage: int = 7) -> int:
        heute = datetime.now(self.g.tz).date()
        neu = 0
        for i in range(tage, 0, -1):
            d = heute - timedelta(days=i)
            ref = f"tag:{d.isoformat()}"
            if await self.store.eine("SELECT 1 AS x FROM erinnerungen WHERE quelle_ref = %s", (ref,)):
                continue
            text, fremd = await self._tagestext(d)
            if text:
                ende = datetime.combine(d, datetime.max.time().replace(microsecond=0), self.g.tz)
                if await self.g.speichern(text, "episodisch", ende, quelle="rueckblick", quelle_ref=ref, fremd=fremd,
                                          daten={"tag": d.isoformat()}):
                    neu += 1
        return neu

    async def _tagestext(self, d: date) -> tuple[str | None, bool]:
        """Text des Rückblicks und ob er fremden Text (Betreffzeilen, Termintitel, Dateien) enthält."""
        start = datetime.combine(d, datetime.min.time(), self.g.tz)
        ende = start + timedelta(days=1)
        rows = await self.store.alle(
            """SELECT e.quelle, e.text, e.daten, e.fremd, o.name AS ort FROM erinnerungen e LEFT JOIN orte o ON o.id = e.ort_id
               WHERE e.zeit >= %s AND e.zeit < %s AND e.art = 'episodisch' AND e.quelle NOT IN ('rueckblick')
                 AND coalesce(e.quelle_ref, '') NOT LIKE 'fototag:%%' ORDER BY e.zeit""", (start, ende))
        erledigt = await self.store.alle("SELECT titel FROM aufgaben WHERE erledigt >= %s AND erledigt < %s", (start, ende))
        if not rows and not erledigt:
            return None, False
        teile = []
        termine = [r["daten"].get("titel") or r["text"] for r in rows if r["quelle"] == "kalender"]
        if termine:
            teile.append("Termine: " + ", ".join(termine[:8]))
        mails = [r for r in rows if r["quelle"] == "mail"]
        if mails:
            von = list(dict.fromkeys(r["daten"].get("absender") or r["daten"].get("absender_mail") for r in mails))
            teile.append(f"{len(mails)} Mails (von {', '.join(v for v in von[:5] if v)})")
        fotos = [r for r in rows if r["quelle"] == "foto"]
        if fotos:
            orte = list(dict.fromkeys(r["ort"] for r in fotos if r["ort"]))
            teile.append(f"{len(fotos)} Fotos" + (f" ({', '.join(orte[:4])})" if orte else ""))
        dateien = [r["text"].split(":")[0].removeprefix("Datei ") for r in rows if r["quelle"] == "datei"]
        if dateien:
            teile.append("Dateien: " + ", ".join(dateien[:5]))
        sonst = [r["text"] for r in rows if r["quelle"] not in ("kalender", "mail", "foto", "datei")]
        if sonst:
            teile.append("Notiert: " + "; ".join(t[:80] for t in sonst[:5]))
        if erledigt:
            teile.append("Erledigt: " + ", ".join(a["titel"] for a in erledigt[:8]))
        # Termine sind eigene Daten (fremd nur Einladungen, das steht dann in r["fremd"]); Mails und Dateien nicht
        fremd = bool(mails or dateien) or any(r["fremd"] for r in rows if r["quelle"] not in ("foto",))
        return f"Rückblick {WOCHENTAG_NAMEN[d.weekday()]}, {d.strftime('%d.%m.%Y')}: " + ". ".join(teile), fremd


_FEIERTAGE = {name.lower() for _, name, _ in feiertage_jahr(2026)} | {
    "heiligabend", "silvester", "halloween", "valentinstag", "muttertag", "vatertag", "nikolaus", "nikolaustag",
    "erster advent", "zweiter advent", "dritter advent", "vierter advent", "totensonntag", "volkstrauertag",
    "st. martin", "beginn der sommerzeit", "ende der sommerzeit", "rosenmontag", "aschermittwoch", "gründonnerstag"}


def _feiertag(t: dict) -> bool:
    """Ganztägiger Eintrag aus einem Feiertags-Kalender oder mit dem Namen eines Feiertags."""
    if not t.get("ganztags"):
        return False
    quelle = (t.get("quelle") or "").lower()
    titel = " ".join((t.get("titel") or "").lower().replace("(regional)", "").split())
    return any(w in quelle for w in ("feiertag", "holiday", "ferien")) or titel in _FEIERTAGE or \
        any(titel.startswith(f) for f in _FEIERTAGE if len(f) > 6)
