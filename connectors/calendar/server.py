"""Connector Kalender: spiegelt ICS-Kalender in den lokalen Termin-Cache (Postgres).

CALENDAR_ICS_URLS="Privat=https://...ics;Arbeit=https://...ics"
Die Links sind die "geheimen iCal-Adressen" aus Google Kalender, iCloud, Nextcloud oder Outlook.
Gesucht wird immer im Cache, deshalb funktioniert calendar.search auch offline.
Neue Termine (calendar.create) bleiben lokal, ICS-Links sind nur lesbar.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import date, datetime, time, timedelta

import icalendar
import recurring_ical_events
from mcp.server.mcpserver import MCPServer
from qulsi_connector import TZ, db, im_hintergrund, internet, run

log = logging.getLogger("calendar")
SYNC_MINUTEN = float(os.environ.get("CALENDAR_SYNC_MINUTES", "15"))
# Deine eigenen Adressen: Termine mit anderem ORGANIZER sind Einladungen von Dritten (extern)
EIGENE = {a.strip().lower().removeprefix("mailto:") for a in
          (os.environ.get("CALENDAR_EIGENE_ADRESSEN") or os.environ.get("MAIL_FROM", "")).replace(",", ";").split(";")
          if a.strip()}
_letzter_stand: dict[str, str] = {}


def kalender() -> dict[str, str]:
    out = {}
    for teil in filter(None, (s.strip() for s in os.environ.get("CALENDAR_ICS_URLS", "").split(";"))):
        name, _, url = teil.partition("=") if "=" in teil.split("://")[0] else ("Kalender", "", teil)
        out[name.strip()] = url.strip().replace("webcal://", "https://", 1)
    return out


def _zeit(wert) -> tuple[datetime, bool]:
    if isinstance(wert, datetime):
        return (wert if wert.tzinfo else wert.replace(tzinfo=TZ)), False
    return datetime.combine(wert, time.min, TZ), True  # ganztägig


def extern(ev, eigene: set[str] | None = None) -> bool:
    """Hat ein Dritter den Termin angelegt? Ohne ORGANIZER ist es ein eigener Termin."""
    org = ev.get("ORGANIZER")
    if not org:
        return False
    adresse = str(org).strip().lower().removeprefix("mailto:")
    return adresse not in (EIGENE if eigene is None else eigene)


def termine_aus_ics(daten: bytes, von: date, bis: date) -> list[dict]:
    cal = icalendar.Calendar.from_ical(daten)
    out = []
    for ev in recurring_ical_events.of(cal).between(von, bis):
        start, ganztags = _zeit(ev.get("DTSTART").dt)
        ende = _zeit(ev.get("DTEND").dt)[0] if ev.get("DTEND") else None
        out.append({
            "uid": str(ev.get("UID", "")) or hashlib.sha1(str(ev.get("SUMMARY")).encode()).hexdigest(),
            "titel": str(ev.get("SUMMARY", "(ohne Titel)")),
            "start": start, "ende": ende, "ganztags": ganztags,
            "ort": str(ev.get("LOCATION")) if ev.get("LOCATION") else None,
            "beschreibung": str(ev.get("DESCRIPTION"))[:1000] if ev.get("DESCRIPTION") else None,
            "extern": extern(ev),
        })
    return out


async def sync() -> str:
    heute = datetime.now(TZ).date()
    von, bis = heute - timedelta(days=30), heute + timedelta(days=365)
    berichte = []
    for name, url in kalender().items():
        quelle = f"ics:{name}"
        try:
            async with internet(timeout=60, follow_redirects=True) as c:
                r = await c.get(url)
                r.raise_for_status()
            termine = termine_aus_ics(r.content, von, bis)
        except Exception as exc:  # noqa: BLE001
            log.warning("Kalender %s nicht abrufbar: %s", name, exc)
            berichte.append(f"{name}: nicht erreichbar, Cache bleibt")
            continue
        async with await db() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM termine WHERE quelle = %s AND start >= %s",
                                   (quelle, datetime.combine(von, time.min, TZ)))
                for t in termine:
                    await conn.execute(
                        """INSERT INTO termine (quelle, uid, titel, start, ende, ganztags, ort, beschreibung, extern)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (quelle, uid, start) DO UPDATE SET titel = EXCLUDED.titel, ende = EXCLUDED.ende,
                             ort = EXCLUDED.ort, beschreibung = EXCLUDED.beschreibung, extern = EXCLUDED.extern,
                             aktualisiert = now()""",
                        (quelle, t["uid"], t["titel"], t["start"], t["ende"], t["ganztags"], t["ort"], t["beschreibung"],
                         t["extern"]))
            stand = hashlib.sha1(json.dumps(termine, default=str, sort_keys=True).encode()).hexdigest()
            if _letzter_stand.get(name) != stand:
                _letzter_stand[name] = stand
                await conn.execute(
                    "INSERT INTO ereignisse (art, quelle, text) VALUES ('kalender_sync', 'connector:calendar', %s)",
                    (f"Kalender {name} aktualisiert: {len(termine)} Termine",))
        berichte.append(f"{name}: {len(termine)} Termine")
    return "; ".join(berichte) or "Keine Kalender eingetragen (CALENDAR_ICS_URLS)."


mcp = MCPServer("calendar", instructions="Termine aus dem lokalen Kalender-Cache.")


@mcp.tool()
async def calendar_search(tag: str | None = None, tage: int = 1, suche: str | None = None) -> list[dict]:
    """Termine ab einem Tag. Funktioniert auch offline (lokaler Cache).

    Args:
        tag: Startdatum JJJJ-MM-TT, Standard heute
        tage: wie viele Tage ab Startdatum (1 = nur dieser Tag, höchstens 366)
        suche: optionaler Suchbegriff im Titel oder Ort
    """
    d = date.fromisoformat(tag) if tag else datetime.now(TZ).date()
    start = datetime.combine(d, time.min, TZ)
    ende = start + timedelta(days=max(1, min(tage, 366)))  # "nächster Termin bei X" braucht mehr als zwei Monate
    async with await db() as conn:
        rows = await (await conn.execute(
            """SELECT titel, start, ende, ganztags, ort, quelle FROM termine
               WHERE start < %s AND coalesce(ende, start) > %s
                 AND (%s::text IS NULL OR titel ILIKE '%%' || %s || '%%' OR ort ILIKE '%%' || %s || '%%')
               ORDER BY start LIMIT 100""", (ende, start, suche, suche, suche))).fetchall()
    out = []
    for r in rows:
        s = r["start"].astimezone(TZ)
        out.append({
            "titel": r["titel"], "tag": s.strftime("%a %d.%m."),
            "von": "ganztägig" if r["ganztags"] else s.strftime("%H:%M"),
            "bis": None if r["ganztags"] or not r["ende"] else r["ende"].astimezone(TZ).strftime("%H:%M"),
            "ort": r["ort"], "kalender": r["quelle"].removeprefix("ics:"),
        })
    return out


@mcp.tool()
async def calendar_create(titel: str, start: str, ende: str | None = None, ort: str | None = None) -> str:
    """Trägt einen Termin in den lokalen QuLSI-Kalender ein.

    Args:
        titel: Titel des Termins
        start: Beginn, JJJJ-MM-TTTHH:MM (oder JJJJ-MM-TT für ganztägig)
        ende: Ende im selben Format, Standard eine Stunde nach Beginn
        ort: optional
    """
    ganztags = len(start) == 10
    s = datetime.fromisoformat(start)
    s = s.replace(tzinfo=TZ) if s.tzinfo is None else s
    e = datetime.fromisoformat(ende).replace(tzinfo=TZ) if ende else s + (timedelta(days=1) if ganztags else timedelta(hours=1))
    async with await db() as conn:
        await conn.execute(
            "INSERT INTO termine (quelle, uid, titel, start, ende, ganztags, ort) VALUES ('lokal', %s, %s, %s, %s, %s, %s)",
            (str(uuid.uuid4()), titel, s, e, ganztags, ort))
        await conn.execute("INSERT INTO ereignisse (art, quelle, text) VALUES ('termin', 'connector:calendar', %s)",
                           (f"Termin eingetragen: {titel} am {s:%d.%m. %H:%M}",))
    return f"Eingetragen: {titel} am {s:%d.%m.%Y}" + ("" if ganztags else f" um {s:%H:%M}") + "."


@mcp.tool()
async def calendar_sync() -> str:
    """Holt die Kalender jetzt neu (braucht Internet)."""
    return await sync()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    im_hintergrund(sync, SYNC_MINUTEN)
    run(mcp)
