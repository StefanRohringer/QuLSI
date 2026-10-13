"""Connector Kameras: aktuelles Bild auswerten und Bildausschnitte prüfen lassen.

Bilder verlassen den Vision-Dienst nicht: zurück kommen nur Fakten (was liegt wo).
"""

from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer
from qulsi_connector import intern, run

VISION_URL = os.environ.get("VISION_URL", "http://vision:8200").rstrip("/")

mcp = MCPServer("vision", instructions="Kameras und Bilderkennung.")


@mcp.tool()
async def camera_capture(kamera: str) -> dict:
    """Holt ein aktuelles Bild einer Kamera, erkennt Gegenstände und aktualisiert die Sichtungen.

    Args:
        kamera: Kameraname aus der Konfiguration, z. B. "kueche".
    """
    async with intern(timeout=90) as c:
        r = await c.post(f"{VISION_URL}/snapshot/{kamera}")
    return r.json() if r.status_code < 400 else {"fehler": r.text[:300]}


@mcp.tool()
async def vision_analyze(frage: str, sighting_id: int | None = None, kamera: str | None = None) -> str:
    """Lässt das Bildmodell einen Bildausschnitt (sighting_id) oder ein aktuelles Kamerabild prüfen.

    Args:
        frage: z. B. "Ist das mein Schlüsselbund?" oder "Steht das Fenster offen?"
        sighting_id: aus objects_where
        kamera: alternativ ein aktuelles Bild dieser Kamera
    """
    async with intern(timeout=180) as c:
        r = await c.post(f"{VISION_URL}/analyze", json={"frage": frage, "sighting_id": sighting_id, "kamera": kamera})
    return r.json().get("antwort", "") if r.status_code < 400 else f"Fehler: {r.text[:300]}"


if __name__ == "__main__":
    run(mcp)
