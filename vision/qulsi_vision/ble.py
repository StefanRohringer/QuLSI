"""Bluetooth-Anhänger über ESPresense (MQTT).

ESPresense-Empfänger in jedem Raum melden je Anhänger die geschätzte Entfernung unter
espresense/devices/<anhänger-id>/<raum>. Der Raum mit der kleinsten aktuellen Entfernung
gilt als Aufenthaltsort und wird als Sichtung mit quelle='ble' gespeichert.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from .store import Store

log = logging.getLogger(__name__)

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
GUELTIG_S = 30  # Messungen älter als das zählen nicht mehr


class RaumSchaetzer:
    """Reine Logik, ohne MQTT: welcher Raum ist für einen Anhänger am nächsten?"""

    def __init__(self, raeume: dict[str, str]):
        self.raeume = {k.lower(): v for k, v in raeume.items()}
        self.messungen: dict[str, dict[str, tuple[float, float]]] = {}  # tag -> raum -> (distanz, zeit)

    def messung(self, tag: str, raum_id: str, distanz: float, jetzt: float | None = None) -> None:
        raum = self.raeume.get(raum_id.lower())
        if raum is None:
            return
        self.messungen.setdefault(tag, {})[raum] = (distanz, jetzt or time.time())

    def raum(self, tag: str, jetzt: float | None = None) -> tuple[str, float] | None:
        jetzt = jetzt or time.time()
        aktuell = [(d, r) for r, (d, t) in self.messungen.get(tag, {}).items() if jetzt - t <= GUELTIG_S]
        if not aktuell:
            return None
        d, r = min(aktuell)
        return r, d


async def ble_schleife(store: Store, raeume: dict[str, str]) -> None:
    import paho.mqtt.client as mqtt

    schaetzer = RaumSchaetzer(raeume)
    queue: asyncio.Queue[tuple[str, str, float]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_message(_c, _u, msg):
        try:
            _, _, tag, raum_id = msg.topic.split("/", 3)
            distanz = float(json.loads(msg.payload).get("distance", 99))
        except (ValueError, json.JSONDecodeError):
            return
        loop.call_soon_threadsafe(queue.put_nowait, (tag, raum_id, distanz))

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.on_message = on_message
    client.on_connect = lambda c, *_: c.subscribe("espresense/devices/#")
    client.connect_async(MQTT_HOST, MQTT_PORT)
    client.loop_start()
    log.info("BLE: lausche auf %s:%s", MQTT_HOST, MQTT_PORT)

    zuletzt_geschrieben: dict[str, tuple[str, float]] = {}
    while True:
        tag, raum_id, distanz = await queue.get()
        schaetzer.messung(tag, raum_id, distanz)
        ergebnis = schaetzer.raum(tag)
        if ergebnis is None:
            continue
        raum, d = ergebnis
        vorher = zuletzt_geschrieben.get(tag)
        # In die DB nur bei Raumwechsel oder alle 60 s, sonst wäre das jede Sekunde ein Schreibvorgang.
        if vorher and vorher[0] == raum and time.time() - vorher[1] < 60:
            continue
        obj = await store.objekt_per_ble(tag)
        if obj is None:
            continue
        zone = await store.raum_zone(raum)
        sicherheit = max(0.3, min(0.95, 1.0 - d / 10))
        await store.sichtung(object_id=obj["id"], label="ble", zone=zone, kamera=None, sicherheit=sicherheit,
                             aehnlichkeit=None, quelle="ble", name_fuer_ereignis=obj["name"])
        zuletzt_geschrieben[tag] = (raum, time.time())
