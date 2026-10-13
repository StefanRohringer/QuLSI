"""Connector Home Assistant: Geräte lesen und schalten, Türen nur mit Freigabe."""

from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer
from qulsi_connector import geheimnis_aktuell, internet, run

HA_URL = os.environ.get("HA_URL", "").rstrip("/")

# Was home.set_state darf. Schlösser, Alarmanlage und Garagentore fehlen absichtlich:
# Türen gehen nur über door.unlock (high, Freigabe per Handy).
ERLAUBT = {
    "light": {"turn_on", "turn_off", "toggle"},
    "switch": {"turn_on", "turn_off", "toggle"},
    "fan": {"turn_on", "turn_off", "toggle", "set_percentage"},
    "media_player": {"turn_on", "turn_off", "media_play", "media_pause", "media_stop", "volume_set", "media_next_track"},
    "climate": {"set_temperature", "set_hvac_mode", "turn_on", "turn_off"},
    "cover": {"open_cover", "close_cover", "stop_cover", "set_cover_position"},
    "scene": {"turn_on"},
    "script": {"turn_on"},
}

mcp = MCPServer("home_assistant", instructions="Steuert das Zuhause über Home Assistant.")


def _ha():
    if not HA_URL:
        raise RuntimeError("Home Assistant ist nicht eingerichtet (HA_URL fehlt in der .env).")
    return internet(base_url=HA_URL, headers={"Authorization": f"Bearer {geheimnis_aktuell('HA_TOKEN')}"}, timeout=20)


async def _anfrage(methode: str, pfad: str, **kw):
    """Fehler als lesbaren Text statt als Ausnahme (MCP würde sie sonst verschlucken)."""
    try:
        async with _ha() as ha:
            r = await ha.request(methode, pfad, **kw)
    except Exception as exc:  # noqa: BLE001
        return None, f"Fehler: Home Assistant nicht erreichbar ({exc})"
    if r.status_code >= 400:
        return None, f"Fehler: Home Assistant meldet {r.status_code}: {r.text[:200]}"
    return r, None


@mcp.tool()
async def home_get_state(suche: str) -> list[dict] | str:
    """Zustand von Geräten, gesucht nach Name, Raum oder entity_id (z. B. "wohnzimmer", "light.kueche")."""
    r, fehler = await _anfrage("GET", "/api/states")
    if fehler:
        return fehler
    s = suche.lower()
    out = []
    for st in r.json():
        name = st["attributes"].get("friendly_name", "")
        if s in st["entity_id"].lower() or s in name.lower():
            out.append({"entity_id": st["entity_id"], "name": name, "zustand": st["state"]})
    return out[:30]


@mcp.tool()
async def home_set_state(entity_id: str, aktion: str, daten: dict | None = None) -> str:
    """Schaltet Licht, Steckdosen, Rollläden, Musik, Heizung oder Szenen.

    Args:
        entity_id: z. B. "light.wohnzimmer"
        aktion: Home-Assistant-Dienst, z. B. "turn_on", "turn_off", "set_temperature"
        daten: optionale Parameter, z. B. {"brightness_pct": 40} oder {"temperature": 21}
    """
    domain = entity_id.split(".", 1)[0]
    if aktion not in ERLAUBT.get(domain, set()):
        return f"Nicht erlaubt: {domain}.{aktion}. Erlaubt sind {sorted(ERLAUBT)} mit festen Aktionen."
    _, fehler = await _anfrage("POST", f"/api/services/{domain}/{aktion}", json={"entity_id": entity_id, **(daten or {})})
    if fehler:
        return fehler
    return f"Erledigt: {domain}.{aktion} für {entity_id}."


@mcp.tool()
async def door_unlock(entity_id: str) -> str:
    """Entriegelt ein Türschloss (lock.*). Kritisch: läuft nur nach Freigabe in der Handy-App."""
    if not entity_id.startswith("lock."):
        return "Nur lock.* Entitäten."
    _, fehler = await _anfrage("POST", "/api/services/lock/unlock", json={"entity_id": entity_id})
    return fehler or f"{entity_id} entriegelt."


if __name__ == "__main__":
    run(mcp)
