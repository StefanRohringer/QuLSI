"""HTTP-Schnittstelle der Hintergrund-Agents (für App und Skripte).

- GET  /api/agenten                   Übersicht: Zeitplan, aktiv, letzter und nächster Lauf
- GET  /api/agenten/{name}/laeufe     die letzten Läufe eines Agents
- POST /api/agenten/{name}/starten    jetzt laufen lassen (wartet auf das Ergebnis)
- POST /api/agenten/{name}/aktiv      {"aktiv": true|false} ein- oder ausschalten

Welche Werkzeuge ein Agent hat und was er selbstständig darf, steht nur in
config/agenten.yaml. Über die API lässt sich das absichtlich nicht ändern.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .laeufer import Hintergrund

router = APIRouter(prefix="/api/agenten")


def _hg(request: Request, name: str | None = None) -> Hintergrund:
    hg: Hintergrund | None = getattr(request.app.state, "hintergrund", None)
    if hg is None:
        raise HTTPException(503, "Hintergrund-Agents laufen nicht")
    if name is not None and name not in hg.agenten:
        raise HTTPException(404, f"Agent {name} gibt es nicht")
    return hg


@router.get("")
async def uebersicht(request: Request):
    hg = _hg(request)
    return {"an": hg.an, "agenten": await hg.uebersicht()}


@router.get("/{name}/laeufe")
async def laeufe(request: Request, name: str, anzahl: int = 20):
    hg = _hg(request, name)
    rows = await hg.store.alle(
        """SELECT id, ausloeser, start, ende, status, ergebnis, werkzeuge FROM agent_laeufe
           WHERE agent = %s ORDER BY start DESC LIMIT %s""", (name, min(anzahl, 100)))
    return [{**r, "start": r["start"].isoformat(), "ende": r["ende"].isoformat() if r["ende"] else None} for r in rows]


@router.post("/{name}/starten")
async def starten(request: Request, name: str):
    return await _hg(request, name).ausfuehren(name, "manuell")


@router.post("/{name}/aktiv")
async def aktiv(request: Request, name: str):
    body = await request.json()
    if not isinstance(body.get("aktiv"), bool):
        raise HTTPException(400, "aktiv muss true oder false sein")
    hg = _hg(request, name)
    await hg.schalten(name, body["aktiv"])
    return {"name": name, "aktiv": body["aktiv"]}
