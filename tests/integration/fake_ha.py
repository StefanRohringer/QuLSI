"""Ersatz für Dienste "draußen" in den Tests (nur über den Egress-Proxy erreichbar):
- Home Assistant: /api/states und /api/services
- ein ICS-Kalender: /kalender.ics (Termine relativ zu heute)
- ein SMTP-Server auf Port 2525, gesendete Mails unter /test/mails
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response

MAILS: list[str] = []


async def smtp(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Sehr einfacher SMTP-Server ohne TLS und Anmeldung."""
    writer.write(b"220 fake-smtp\r\n")
    daten = False
    puffer = b""
    while line := await reader.readline():
        if daten:
            if line == b".\r\n":
                MAILS.append(puffer.decode(errors="replace"))
                puffer, daten = b"", False
                writer.write(b"250 OK\r\n")
            else:
                puffer += line
            continue
        cmd = line[:4].upper()
        if cmd == b"EHLO":
            writer.write(b"250-fake-smtp\r\n250 8BITMIME\r\n")
        elif cmd == b"DATA":
            daten = True
            writer.write(b"354 los\r\n")
        elif cmd == b"QUIT":
            writer.write(b"221 tschuess\r\n")
            await writer.drain()
            break
        else:
            writer.write(b"250 OK\r\n")
        await writer.drain()
    writer.close()


@asynccontextmanager
async def lifespan(app):
    server = await asyncio.start_server(smtp, "0.0.0.0", 2525)
    yield
    server.close()


app = FastAPI(lifespan=lifespan)
AUFRUFE: list[dict] = []
STATES = [
    {"entity_id": "light.wohnzimmer", "state": "off", "attributes": {"friendly_name": "Wohnzimmer Licht"}},
    {"entity_id": "lock.haustuer", "state": "locked", "attributes": {"friendly_name": "Haustür"}},
    {"entity_id": "calendar.familie", "state": "off", "attributes": {"friendly_name": "Familie"}},
]


def _auth(authorization: str):
    if authorization != "Bearer ha-test":
        raise HTTPException(401)


@app.get("/api/states")
async def states(authorization: str = Header("")):
    _auth(authorization)
    return STATES


@app.post("/api/services/{domain}/{service}")
async def service(domain: str, service: str, request: Request, authorization: str = Header("")):
    _auth(authorization)
    body = await request.json()
    AUFRUFE.append({"domain": domain, "service": service, "body": body})
    if domain == "calendar":
        return {"changed_states": [], "service_response": {"calendar.familie": {"events": [
            {"summary": "Zahnarzt", "start": "2026-10-07T16:00:00+02:00", "end": "2026-10-07T17:00:00+02:00"}]}}}
    for s in STATES:
        if s["entity_id"] == body.get("entity_id"):
            s["state"] = {"turn_on": "on", "turn_off": "off", "unlock": "unlocked"}.get(service, s["state"])
    return []


@app.get("/test/aufrufe")
async def aufrufe():
    return AUFRUFE


@app.get("/kalender.ics")
async def kalender():
    heute = datetime.now(ZoneInfo("Europe/Berlin")).date()  # wie der Core, sonst fehlt kurz nach Mitternacht "heute"
    vor_einer_woche = heute - timedelta(days=7)
    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//qulsi-test//DE
BEGIN:VEVENT
UID:zahnarzt-1
DTSTART;TZID=Europe/Berlin:{heute:%Y%m%d}T160000
DTEND;TZID=Europe/Berlin:{heute:%Y%m%d}T170000
SUMMARY:Zahnarzt
LOCATION:Praxis Dr. Weber
END:VEVENT
BEGIN:VEVENT
UID:team-1
DTSTART;TZID=Europe/Berlin:{vor_einer_woche:%Y%m%d}T100000
DTEND;TZID=Europe/Berlin:{vor_einer_woche:%Y%m%d}T103000
RRULE:FREQ=WEEKLY
SUMMARY:Teamrunde
END:VEVENT
BEGIN:VEVENT
UID:muell-1
DTSTART;VALUE=DATE:{heute:%Y%m%d}
DTEND;VALUE=DATE:{heute + timedelta(days=1):%Y%m%d}
SUMMARY:Müll rausbringen
END:VEVENT
END:VCALENDAR
"""
    return Response(ics.replace("\n", "\r\n"), media_type="text/calendar")


@app.get("/test/mails")
async def mails():
    return MAILS
