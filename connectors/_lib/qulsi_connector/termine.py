"""Termine aus Cloud-Kalendern (Google, Microsoft) in den lokalen Termin-Cache schreiben.

Gleiche Tabelle wie der Kalender-Connector: calendar.search, die "Heute"-Ansicht und
proaktive Agents sehen damit alle Kalender, auch offline.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from . import db

_letzter_stand: dict[str, str] = {}


async def ersetzen(quelle: str, termine: list[dict], von: datetime, connector: str, name: str) -> None:
    """Alle Termine dieser Quelle ab `von` durch die neuen ersetzen (eine Transaktion)."""
    async with await db() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM termine WHERE quelle = %s AND start >= %s", (quelle, von))
            for t in termine:
                await conn.execute(
                    # extern: Titel/Beschreibung hat ein Dritter geschrieben (Einladung) -> fremder Text
                    """INSERT INTO termine (quelle, uid, titel, start, ende, ganztags, ort, beschreibung, extern)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (quelle, uid, start) DO UPDATE SET titel = EXCLUDED.titel, ende = EXCLUDED.ende,
                         ort = EXCLUDED.ort, beschreibung = EXCLUDED.beschreibung, extern = EXCLUDED.extern,
                         aktualisiert = now()""",
                    (quelle, t["uid"], t["titel"], t["start"], t["ende"], t["ganztags"], t["ort"], t["beschreibung"],
                     bool(t.get("extern"))))
        stand = hashlib.sha1(json.dumps(termine, default=str, sort_keys=True).encode()).hexdigest()
        if _letzter_stand.get(quelle) != stand:
            _letzter_stand[quelle] = stand
            await conn.execute(
                "INSERT INTO ereignisse (art, quelle, text) VALUES ('kalender_sync', %s, %s)",
                (f"connector:{connector}", f"Kalender {name} aktualisiert: {len(termine)} Termine"))


async def eintragen(quelle: str, t: dict, connector: str) -> None:
    """Einen neu angelegten Termin sofort in den Cache (nicht erst beim nächsten Abgleich)."""
    async with await db() as conn:
        await conn.execute(
            """INSERT INTO termine (quelle, uid, titel, start, ende, ganztags, ort) VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (quelle, uid, start) DO NOTHING""",
            (quelle, t["uid"], t["titel"], t["start"], t["ende"], t["ganztags"], t["ort"]))
        await conn.execute("INSERT INTO ereignisse (art, quelle, text) VALUES ('termin', %s, %s)",
                           (f"connector:{connector}", f"Termin eingetragen: {t['titel']} am {t['start']:%d.%m. %H:%M}"))
