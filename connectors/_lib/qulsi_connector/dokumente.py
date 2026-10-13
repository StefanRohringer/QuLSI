"""Dokumente ins Gedächtnis aufnehmen: Text in Abschnitte, Vektoren nach Qdrant ("dokumente"),
Eintrag in Postgres (Tabelle dokumente), Original in den Object Storage.

Gleiches Format wie beim Dateien-Connector, deshalb findet memory.search im Core alles zusammen.
quelle ist eindeutig und sagt, woher es kommt: "nas:Rechnungen/2026/strom.pdf".
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime

from . import intern, objekt_ablegen

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333").rstrip("/")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://litellm:4000/v1").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "qulsi")
ABSCHNITT, UEBERLAPPUNG = 1200, 150


def abschnitte(text: str) -> list[str]:
    text = " ".join(text.split())
    out, i = [], 0
    while i < len(text):
        out.append(text[i : i + ABSCHNITT])
        i += ABSCHNITT - UEBERLAPPUNG
    return out


async def embed(texte: list[str]) -> list[list[float]]:
    async with intern(timeout=300) as c:
        r = await c.post(f"{LLM_BASE_URL}/embeddings", json={"model": "embed", "input": texte},
                         headers={"Authorization": f"Bearer {LLM_API_KEY}"})
        r.raise_for_status()
        return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]


async def qdrant(methode: str, pfad: str, json: dict | None = None) -> dict:
    async with intern() as c:
        r = await c.request(methode, f"{QDRANT_URL}{pfad}", json=json)
        r.raise_for_status()
        return r.json()


async def collection() -> None:
    if not (await qdrant("GET", "/collections/dokumente/exists"))["result"]["exists"]:
        dim = int(os.environ.get("EMBED_DIM", "1024"))
        await qdrant("PUT", "/collections/dokumente", {"vectors": {"size": dim, "distance": "Cosine"}})


async def bekannt(conn, praefix: str) -> dict[str, datetime | None]:
    rows = await (await conn.execute(
        "SELECT quelle, geaendert FROM dokumente WHERE quelle LIKE %s", (praefix + "%",))).fetchall()
    return {r["quelle"]: r["geaendert"] for r in rows}


async def aufnehmen(conn, quelle: str, titel: str, daten: bytes, text: str, endung: str, geaendert: datetime) -> int:
    """Ein Dokument (neu oder geändert) indizieren. Gibt die Zahl der Abschnitte zurück."""
    teile = abschnitte(text)
    key = objekt_ablegen(daten, endung)
    row = await (await conn.execute(
        # geaendert erst nach erfolgreichem Index setzen, sonst gilt das Dokument als erledigt
        """INSERT INTO dokumente (quelle, titel, objekt_key, mime, groesse, geaendert, indiziert)
           VALUES (%s, %s, %s, %s, %s, NULL, now())
           ON CONFLICT (quelle) DO UPDATE SET objekt_key = EXCLUDED.objekt_key, groesse = EXCLUDED.groesse,
             geaendert = NULL, indiziert = now() RETURNING id""",
        (quelle, titel, key, endung.lstrip("."), len(daten)))).fetchone()
    await qdrant("POST", "/collections/dokumente/points/delete",
                 {"filter": {"must": [{"key": "quelle", "match": {"value": quelle}}]}})
    for start in range(0, len(teile), 16):
        stueck = teile[start : start + 16]
        vektoren = await embed(stueck)
        punkte = [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{quelle}#{start + i}")), "vector": v,
                   "payload": {"quelle": quelle, "titel": titel, "dokument_id": row["id"],
                               "abschnitt": start + i, "text": t}}
                  for i, (t, v) in enumerate(zip(stueck, vektoren))]
        await qdrant("PUT", "/collections/dokumente/points?wait=true", {"points": punkte})
    await conn.execute("UPDATE dokumente SET geaendert = %s WHERE id = %s", (geaendert, row["id"]))
    return len(teile)


async def entfernen(conn, quelle: str) -> None:
    await conn.execute("DELETE FROM dokumente WHERE quelle = %s", (quelle,))
    await qdrant("POST", "/collections/dokumente/points/delete",
                 {"filter": {"must": [{"key": "quelle", "match": {"value": quelle}}]}})


async def suchen(suche: str, praefix: str, anzahl: int = 5) -> list[dict]:
    """Bedeutungssuche, nur in Dokumenten mit diesem Quellen-Präfix."""
    v = (await embed([suche]))[0]
    res = await qdrant("POST", "/collections/dokumente/points/query",
                       {"query": v, "limit": anzahl * 4, "with_payload": True})
    out = []
    for p in res["result"]["points"]:
        pl = p["payload"]
        if pl["quelle"].startswith(praefix):
            out.append({"datei": pl["quelle"].removeprefix(praefix), "abschnitt": pl["text"][:400],
                        "score": round(p["score"], 3)})
    return out[:anzahl]
