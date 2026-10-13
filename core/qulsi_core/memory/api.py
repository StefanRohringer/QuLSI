"""HTTP-Schnittstelle des Gedächtnisses für Web-UI und Handy-App (Schutz per API-Schlüssel kommt aus main.py)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


def _kern(request: Request):
    return request.app.state.hub.kern


@router.get("/api/gedaechtnis/finden")
async def finden(request: Request, frage: str = "", person: str | None = None, ort: str | None = None,
                 von: str | None = None, bis: str | None = None, art: str | None = None, anzahl: int = 20):
    try:
        return await _kern(request).gedaechtnis.finden(frage, person, ort, von, bis, art, None, anzahl)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/api/gedaechtnis/timeline")
async def timeline(request: Request, tag: str | None = None, von: str | None = None, bis: str | None = None,
                   person: str | None = None, ort: str | None = None):
    try:
        return await _kern(request).gedaechtnis.timeline(tag, von, bis, person, ort)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/api/gedaechtnis/orte")
async def orte(request: Request):
    return await _kern(request).gedaechtnis.orte_liste()


@router.get("/api/gedaechtnis/status")
async def status(request: Request):
    k = _kern(request)
    await k.gedaechtnis.bereit()
    zahlen = await k.store.eine(
        """SELECT (SELECT count(*) FROM erinnerungen) AS erinnerungen, (SELECT count(*) FROM personen) AS personen,
                  (SELECT count(*) FROM orte) AS orte, (SELECT count(*) FROM fotos) AS fotos,
                  (SELECT count(*) FROM projekte) AS projekte, (SELECT count(*) FROM beziehungen) AS beziehungen,
                  (SELECT count(*) FROM erinnerungen WHERE NOT eingebettet) AS ohne_vektor""")
    return {**zahlen, "organisator": k.organisator.letzter_lauf, "memchip": await k.memchip.status()}


@router.post("/api/gedaechtnis/organisieren")
async def organisieren(request: Request):
    return await _kern(request).organisator.lauf()
