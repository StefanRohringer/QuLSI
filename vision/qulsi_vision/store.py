"""Speicher von Vision: Fakten in Postgres (Rolle qulsi_vision), Fingerabdrücke in Qdrant
(Collection "objekte"), Bildausschnitte im Object Storage. Kein Video, keine Vollbilder."""

from __future__ import annotations

import json
import os

import hashlib
from pathlib import Path

import numpy as np
import psycopg
from psycopg.rows import dict_row
from qdrant_client import AsyncQdrantClient, models

# Liegt ein Gegenstand länger als das nicht mehr an derselben Stelle, beginnt eine neue Sichtung.
NEUE_SICHTUNG_NACH_MIN = int(os.environ.get("NEUE_SICHTUNG_NACH_MIN", "240"))


OBJECT_STORE_DIR = Path(os.environ.get("OBJECT_STORE_DIR", "/data/objects"))
COLLECTION = "objekte"


def objekt_ablegen(daten: bytes, endung: str = ".jpg") -> str:
    """Inhaltsadressiert ablegen, wie der Object Storage im Core."""
    h = hashlib.sha256(daten).hexdigest()
    key = f"{h[:2]}/{h}{endung}"
    ziel = OBJECT_STORE_DIR / key
    if not ziel.exists():
        ziel.parent.mkdir(parents=True, exist_ok=True)
        tmp = ziel.with_suffix(ziel.suffix + ".tmp")
        tmp.write_bytes(daten)
        tmp.replace(ziel)
    return key


def objekt_holen(key: str) -> bytes | None:
    pfad = (OBJECT_STORE_DIR / key).resolve()
    if OBJECT_STORE_DIR.resolve() not in pfad.parents or not pfad.exists():
        return None
    return pfad.read_bytes()


class Store:
    def __init__(self, url: str, qdrant: AsyncQdrantClient | None = None):
        self.url = url
        self.qdrant = qdrant or AsyncQdrantClient(url=os.environ.get("QDRANT_URL", "http://qdrant:6333"))
        self._collection_da = False

    async def _collection(self, dim: int) -> None:
        if self._collection_da:
            return
        if not await self.qdrant.collection_exists(COLLECTION):
            await self.qdrant.create_collection(
                COLLECTION, vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE))
        self._collection_da = True

    async def conn(self) -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(self.url, row_factory=dict_row, autocommit=True)

    async def zonen_abgleichen(self, kameras: list[dict], ble_raeume: dict[str, str]) -> dict[str, list[dict]]:
        """Schreibt die Zonen aus cameras.yaml in die DB und liefert sie je Kamera mit id zurück."""
        out: dict[str, list[dict]] = {}
        async with await self.conn() as c:
            for k in kameras:
                out[k["name"]] = []
                for z in k.get("zonen", []):
                    cur = await c.execute(
                        """INSERT INTO zones (name, raum, kamera, polygon) VALUES (%s, %s, %s, %s)
                           ON CONFLICT (name, kamera) DO UPDATE SET raum = EXCLUDED.raum, polygon = EXCLUDED.polygon
                           RETURNING id""",
                        (z["name"], k["raum"], k["name"], json.dumps(z["polygon"])),
                    )
                    out[k["name"]].append({**z, "id": (await cur.fetchone())["id"], "raum": k["raum"]})
            for raum in ble_raeume.values():
                existing = await (await c.execute("SELECT id FROM zones WHERE name = %s AND kamera IS NULL", (raum,))).fetchone()
                if not existing:
                    await c.execute("INSERT INTO zones (name, raum, kamera) VALUES (%s, %s, NULL)", (raum, raum))
        return out

    async def privacy(self) -> bool:
        async with await self.conn() as c:
            row = await (await c.execute("SELECT value FROM system_state WHERE key = 'privacy_mode'")).fetchone()
        return bool(row and row["value"] is True)

    async def bester_objekt(self, typ: str, fp: np.ndarray, schwelle: float) -> tuple[int | None, float | None]:
        """Ähnlichster angelernter Gegenstand dieses Typs. (None, None), wenn keiner angelernt ist."""
        await self._collection(len(fp))
        res = await self.qdrant.query_points(
            COLLECTION, query=fp.tolist(), limit=1,
            query_filter=models.Filter(must=[models.FieldCondition(key="typ", match=models.MatchValue(value=typ))]))
        if not res.points:
            return None, None
        p = res.points[0]
        return (int(p.id) if p.score >= schwelle else None), float(p.score)

    async def objekt_anlernen(self, name: str, typ: str, aliases: list[str], besitzer: str | None,
                              ble_tag: str | None, fingerabdruck: np.ndarray | None) -> int:
        async with await self.conn() as c:
            cur = await c.execute(
                """INSERT INTO objects (name, typ, aliases, besitzer, ble_tag)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (name) DO UPDATE SET typ = EXCLUDED.typ, aliases = EXCLUDED.aliases,
                       besitzer = EXCLUDED.besitzer, ble_tag = coalesce(EXCLUDED.ble_tag, objects.ble_tag)
                   RETURNING id""",
                (name, typ, aliases, besitzer, ble_tag),
            )
            oid = (await cur.fetchone())["id"]
        if fingerabdruck is not None:
            await self._collection(len(fingerabdruck))
            await self.qdrant.upsert(COLLECTION, [models.PointStruct(
                id=oid, vector=fingerabdruck.tolist(), payload={"typ": typ, "name": name})])
        return oid

    async def sichtung(self, *, object_id: int | None, label: str, zone: dict | None, kamera: str | None,
                       sicherheit: float, aehnlichkeit: float | None, quelle: str, box=None,
                       ausschnitt: str | None = None,
                       neben: list[str] | None = None, name_fuer_ereignis: str | None = None) -> int:
        """Aktualisiert die laufende Sichtung oder legt eine neue an (Gegenstand wurde bewegt)."""
        zone_id = zone["id"] if zone else None
        async with await self.conn() as c:
            if object_id is not None:
                bedingung, param = "s.object_id = %s AND s.quelle = %s", (object_id, quelle)
            else:
                bedingung = ("s.object_id IS NULL AND s.label = %s AND s.kamera IS NOT DISTINCT FROM %s"
                             " AND s.zone_id IS NOT DISTINCT FROM %s")
                param = (label, kamera, zone_id)
            letzte = await (
                await c.execute(
                    f"""SELECT s.id, s.zone_id, s.zuletzt, z.name AS zone FROM sightings s
                        LEFT JOIN zones z ON z.id = s.zone_id WHERE {bedingung}
                        ORDER BY s.zuletzt DESC LIMIT 1""",
                    param,
                )
            ).fetchone()

            gleich = letzte is not None and letzte["zone_id"] == zone_id
            frisch = gleich and (await (await c.execute(
                "SELECT %s > now() - make_interval(mins => %s) AS f", (letzte["zuletzt"], NEUE_SICHTUNG_NACH_MIN)
            )).fetchone())["f"]

            if frisch:
                sid = letzte["id"]
                await c.execute(
                    """UPDATE sightings SET zuletzt = now(), sicherheit = greatest(sicherheit, %s),
                           aehnlichkeit = coalesce(%s, aehnlichkeit), box = coalesce(%s, box),
                           ausschnitt_key = coalesce(%s, ausschnitt_key)
                       WHERE id = %s""",
                    (sicherheit, aehnlichkeit, json.dumps(box) if box else None, ausschnitt, sid),
                )
            else:
                cur = await c.execute(
                    """INSERT INTO sightings (object_id, label, zone_id, kamera, sicherheit, aehnlichkeit, quelle,
                                              box, ausschnitt_key)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                    (object_id, label, zone_id, kamera, sicherheit, aehnlichkeit, quelle,
                     json.dumps(box) if box else None, ausschnitt),
                )
                sid = (await cur.fetchone())["id"]
                if object_id is not None and name_fuer_ereignis:
                    ziel = zone["name"] if zone else "unbekannte Stelle"
                    vorher = f" (vorher {letzte['zone']})" if letzte and letzte["zone"] else ""
                    await c.execute(
                        "INSERT INTO ereignisse (art, quelle, text, daten) VALUES ('objekt_bewegt', 'vision', %s, %s)",
                        (f"{name_fuer_ereignis} jetzt: {ziel}{vorher}",
                         json.dumps({"object_id": object_id, "zone": ziel, "kamera": kamera, "sighting_id": sid})),
                    )
            if neben:
                await c.execute("DELETE FROM relations WHERE sighting_id = %s", (sid,))
                for n in neben:
                    await c.execute("INSERT INTO relations (sighting_id, neben) VALUES (%s, %s) ON CONFLICT DO NOTHING", (sid, n))
        return sid

    async def objekt_per_ble(self, tag: str) -> dict | None:
        async with await self.conn() as c:
            return await (await c.execute("SELECT id, name FROM objects WHERE ble_tag = %s", (tag,))).fetchone()

    async def raum_zone(self, raum: str) -> dict | None:
        async with await self.conn() as c:
            return await (await c.execute("SELECT id, name, raum FROM zones WHERE name = %s AND kamera IS NULL", (raum,))).fetchone()

    async def ausschnitt_key(self, sighting_id: int) -> str | None:
        async with await self.conn() as c:
            row = await (await c.execute("SELECT ausschnitt_key FROM sightings WHERE id = %s", (sighting_id,))).fetchone()
        return row["ausschnitt_key"] if row else None

    async def aufraeumen(self, ausschnitt_tage: int) -> None:
        """Alte Sichtungen löschen, Bildausschnitte nach ausschnitt_tage entfernen (die Fakten bleiben)."""
        async with await self.conn() as c:
            await c.execute("SELECT cleanup_old_data()")
            rows = await (await c.execute(
                """UPDATE sightings SET ausschnitt_key = NULL
                   WHERE ausschnitt_key IS NOT NULL AND zuletzt < now() - make_interval(days => %s)
                   RETURNING ausschnitt_key AS alt""", (ausschnitt_tage,))).fetchall()
        # Inhaltsadressiert: ein Ausschnitt kann (selten) mehrfach verwendet sein, dann bleibt er.
        async with await self.conn() as c:
            for r in rows:
                noch = await (await c.execute("SELECT 1 FROM sightings WHERE ausschnitt_key = %s LIMIT 1", (r["alt"],))).fetchone()
                if not noch:
                    (OBJECT_STORE_DIR / r["alt"]).unlink(missing_ok=True)
