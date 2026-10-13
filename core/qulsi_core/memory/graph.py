"""Kleiner Wissensgraph in Postgres: wer oder was hängt womit zusammen."""

from __future__ import annotations

from .store import Store


class Graph:
    def __init__(self, store: Store):
        self.store = store

    async def verbinden(self, von_typ: str, von: str, relation: str, zu_typ: str, zu: str) -> None:
        async with await self.store.conn() as c:
            await c.execute(
                """INSERT INTO beziehungen (von_typ, von, relation, zu_typ, zu) VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                (von_typ, von, relation, zu_typ, zu),
            )

    async def nachbarn(self, name: str) -> list[dict]:
        """Alle Kanten, die diesen Namen berühren, in beide Richtungen."""
        return await self.store.alle(
            """SELECT von_typ, von, relation, zu_typ, zu FROM beziehungen
               WHERE lower(von) = lower(%s) OR lower(zu) = lower(%s) ORDER BY zeit DESC LIMIT 50""",
            (name, name),
        )
