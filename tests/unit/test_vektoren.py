import asyncio

from qdrant_client import AsyncQdrantClient

from qulsi_core.memory import Vektoren


class Fake(Vektoren):
    async def embed(self, text: str) -> list[float]:
        woerter = ["schlüssel", "küche", "angebot", "max", "kaffee"]
        return [1.0 if w in text.lower() else 0.01 for w in woerter]


def test_qdrant_speichern_und_suchen():
    async def lauf():
        v = Fake("", "", "", dim=5, client=AsyncQdrantClient(location=":memory:"))
        await v.vorbereiten()
        await v.speichern("erinnerungen", 1, "Max trinkt Kaffee schwarz", {"art": "praeferenz"})
        await v.speichern("erinnerungen", 2, "Das Angebot für die Küche kam am Montag", {"art": "episodisch"})
        treffer = await v.suchen("erinnerungen", "Wie mag Max seinen Kaffee?", 1)
        assert treffer[0]["id"] == 1 and treffer[0]["art"] == "praeferenz"
        nur = await v.suchen("erinnerungen", "Kaffee", 5, {"art": "episodisch"})
        assert [t["id"] for t in nur] == [2]

    asyncio.run(lauf())
