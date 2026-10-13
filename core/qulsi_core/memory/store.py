"""Zugriff auf PostgreSQL."""

from __future__ import annotations

import json

import psycopg
from psycopg.rows import dict_row


class Store:
    def __init__(self, url: str):
        self.url = url

    async def conn(self) -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(self.url, row_factory=dict_row, autocommit=True)

    async def alle(self, sql: str, params=None) -> list[dict]:
        async with await self.conn() as c:
            return await (await c.execute(sql, params)).fetchall()

    async def eine(self, sql: str, params=None) -> dict | None:
        async with await self.conn() as c:
            return await (await c.execute(sql, params)).fetchone()

    async def ereignis(self, art: str, text: str, quelle: str = "core", daten: dict | None = None) -> None:
        """Event Bus: in die Timeline schreiben, der Trigger verteilt es per NOTIFY."""
        async with await self.conn() as c:
            await c.execute(
                "INSERT INTO ereignisse (art, quelle, text, daten) VALUES (%s, %s, %s, %s)",
                (art, quelle, text, json.dumps(daten or {}, default=str)),
            )
