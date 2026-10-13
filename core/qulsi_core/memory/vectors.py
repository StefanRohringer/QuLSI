"""Semantische Suche mit Qdrant. Texte werden über LiteLLM (Modell "embed") zu Vektoren."""

from __future__ import annotations

import httpx
from qdrant_client import AsyncQdrantClient, models

COLLECTIONS = {"erinnerungen": "text", "dokumente": "text"}


class Vektoren:
    def __init__(self, qdrant_url: str, llm_base_url: str, llm_api_key: str, dim: int = 1024,
                 embed_model: str = "embed", client: AsyncQdrantClient | None = None):
        self.client = client or AsyncQdrantClient(url=qdrant_url)
        self.llm = llm_base_url.rstrip("/")
        self.key = llm_api_key
        self.dim = dim
        self.model = embed_model

    async def vorbereiten(self) -> None:
        for name in COLLECTIONS:
            if not await self.client.collection_exists(name):
                await self.client.create_collection(
                    name, vectors_config=models.VectorParams(size=self.dim, distance=models.Distance.COSINE)
                )

    async def embed(self, text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{self.llm}/embeddings", json={"model": self.model, "input": text},
                             headers={"Authorization": f"Bearer {self.key}"})
            r.raise_for_status()
            return r.json()["data"][0]["embedding"]

    async def speichern(self, collection: str, punkt_id: int | str, text: str, payload: dict) -> None:
        v = await self.embed(text)
        await self.client.upsert(collection, [models.PointStruct(id=punkt_id, vector=v, payload={**payload, "text": text})])

    async def suchen(self, collection: str, text: str, anzahl: int = 5, filter_: dict | None = None,
                     ids: list[int] | None = None) -> list[dict]:
        """ids schränkt die Suche auf diese Punkte ein (z. B. Erinnerungen, die zu Person/Zeit/Ort passen)."""
        v = await self.embed(text)
        must: list = [models.FieldCondition(key=k, match=models.MatchValue(value=w)) for k, w in (filter_ or {}).items()]
        if ids is not None:
            must.append(models.HasIdCondition(has_id=ids))
        f = models.Filter(must=must) if must else None
        res = await self.client.query_points(collection, query=v, limit=anzahl, query_filter=f, with_payload=True)
        return [{"id": p.id, "score": round(p.score, 4), **(p.payload or {})} for p in res.points]

    async def loeschen_ids(self, collection: str, ids: list[int]) -> None:
        await self.client.delete(collection, points_selector=models.PointIdsList(points=ids))

    async def loeschen(self, collection: str, filter_: dict) -> None:
        f = models.Filter(must=[models.FieldCondition(key=k, match=models.MatchValue(value=w)) for k, w in filter_.items()])
        await self.client.delete(collection, points_selector=models.FilterSelector(filter=f))
