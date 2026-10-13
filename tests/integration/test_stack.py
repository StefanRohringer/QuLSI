"""Ende-zu-Ende-Tests gegen den laufenden QuLSI-Teststack (ohne GPU).

Start:  scripts/test-stack.sh   (startet die Container und ruft diese Tests auf)
"""

from __future__ import annotations

import io
import json
import subprocess
import time
import wave
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
CORE = "http://localhost:8100"
VISION = "http://localhost:8200"
NTFY = "http://localhost:8090"
FAKE_HA = "http://localhost:8123"
KEY = {"Authorization": "Bearer qulsi-test"}
COMPOSE = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.dev.yml",
           "-f", "tests/integration/docker-compose.test.yml"]


def chat(text: str, model: str = "qulsi", verlauf: list[dict] | None = None) -> dict:
    msgs = (verlauf or []) + [{"role": "user", "content": text}]
    r = httpx.post(f"{CORE}/v1/chat/completions", json={"model": model, "messages": msgs}, headers=KEY, timeout=120)
    r.raise_for_status()
    return r.json()


def antwort(r: dict) -> str:
    return r["choices"][0]["message"]["content"]


def sql(query: str) -> str:
    out = subprocess.run(COMPOSE + ["exec", "-T", "postgres", "psql", "-U", "qulsi", "-d", "qulsi", "-tAc", query],
                         cwd=ROOT, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def im_container(dienst: str, code: str) -> subprocess.CompletedProcess:
    return subprocess.run(COMPOSE + ["exec", "-T", dienst, "python", "-c", code], cwd=ROOT, capture_output=True, text=True)


def warten(bedingung, sekunden: float = 60, text: str = "") -> None:
    ende = time.time() + sekunden
    while time.time() < ende:
        if bedingung():
            return
        time.sleep(1)
    raise AssertionError(f"Zeitüberschreitung: {text}")


def ha_aufrufe() -> list[dict]:
    return httpx.get(f"{FAKE_HA}/test/aufrufe").json()


def ntfy_nachrichten() -> list[dict]:
    r = httpx.get(f"{NTFY}/qulsi/json", params={"poll": "1", "since": "all"}, timeout=10)
    return [json.loads(z) for z in r.text.splitlines() if z.strip()]


def freigabe_link(titel: str) -> str:
    msg = [m for m in ntfy_nachrichten() if m.get("title") == titel][-1]
    return next(a for a in msg["actions"] if a["label"] == "Freigeben")["url"]


@pytest.fixture(scope="session", autouse=True)
def angelernt():
    """Schlüsselbund einmal anlernen (Foto + Bluetooth-Anhänger)."""
    with open(ROOT / "config/dev/anlernen_schluessel.jpg", "rb") as f:
        r = httpx.post(
            f"{VISION}/anlernen",
            data={"name": "Schlüsselbund", "typ": "keys", "aliases": "Schlüssel,Hausschlüssel", "ble_tag": "tile_abc"},
            files={"bilder": ("schluessel.jpg", f, "image/jpeg")},
            timeout=60,
        )
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- Grundlagen


def test_frische_box_startet_leer():
    """Neue Installation: leeres Gedächtnis, keine Aufgaben, keine eigenen Termine, kein Name, keine Gespräche."""
    for tabelle, wo in (("erinnerungen", "quelle IN ('gespraech', 'agent')"), ("aufgaben", "true"),
                        ("termine", "quelle = 'lokal'"), ("personen", "true"), ("mail_entwuerfe", "true"),
                        ("gespraeche", "true"), ("confirmations", "true")):
        assert sql(f"SELECT count(*) FROM {tabelle} WHERE {wo}") == "0", tabelle
    assert httpx.get(f"{CORE}/api/nutzer", headers=KEY).json() == {"name": None, "bundesland": None}
    # Box-eigene Schlüssel entstehen beim ersten Start (Web Push in Postgres, Tresor in data/tresor)
    assert sql("SELECT count(*) FROM system_state WHERE key = 'webpush_vapid'") == "1"


def test_anlernen_fingerabdruck_in_qdrant(angelernt):
    assert angelernt["fingerabdruck"] and angelernt["davon_erkannt"] == 1


def test_api_schluessel_noetig():
    assert httpx.post(f"{CORE}/v1/chat/completions", json={"model": "qulsi", "messages": []}).status_code == 401
    assert httpx.get(f"{CORE}/api/heute").status_code == 401


def test_alle_connectoren_und_werkzeuge_geladen():
    h = httpx.get(f"{CORE}/health").json()
    assert all(h["connectoren"].values()), h["connectoren"]
    for f in ("calendar.search", "email.send", "home.set_state", "door.unlock", "files.search",
              "browser.search", "camera.capture", "memory.search", "tasks.create", "persons.search"):
        assert f in h["werkzeuge"]
    assert "email_draft_vorschau" not in h["werkzeuge"]  # internes Werkzeug, nicht fürs Modell
    assert h["offline"] is False


def test_web_app():
    r = httpx.get(f"{CORE}/")
    assert r.status_code == 200 and "QuLSI" in r.text and "Was möchtest du wissen oder erledigen?" in r.text
    assert "?key=" not in r.text and "mitKey" not in r.text  # der App-Schlüssel steht nie in einer Adresse
    assert httpx.get(f"{CORE}/manifest.webmanifest").json()["name"] == "QuLSI"


# ---------------------------------------------------------------- Model Router


def test_router_uhrzeit_ohne_modell():
    r = chat("Wie spät ist es?")
    assert r["qulsi"]["route"] == "system" and r["qulsi"]["modell"] is None
    assert antwort(r).startswith("Es ist ")


def test_feiertag_wird_berechnet_nicht_geraten():
    r = chat("Wann ist der nächste Feiertag?")
    assert r["qulsi"]["werkzeuge"] == ["holidays_list"] and r["qulsi"]["belastet"] is False
    assert antwort(r).startswith("Nächster Feiertag: ") and "20" in antwort(r)


def test_router_kurzer_befehl_kleines_modell():
    vorher = len(ha_aufrufe())
    r = chat("Mach das Licht im Wohnzimmer an", model="qulsi-stimme")
    assert r["qulsi"]["route"] == "klein" and r["qulsi"]["modell"] == "qulsi-klein"
    assert r["qulsi"]["werkzeuge"] == ["home_set_state"]
    assert ha_aufrufe()[vorher:] == [{"domain": "light", "service": "turn_on", "body": {"entity_id": "light.wohnzimmer"}}]


def test_router_analyse_grosses_modell():
    r = chat("Analysiere bitte meine Woche und plane die Termine")
    assert r["qulsi"]["modell"] == "bonsai-think"


# ---------------------------------------------------------------- Vision


def test_kamera_bild_erkennt_persoenlichen_schluessel():
    r = chat("Schau mal in der Küche nach")
    assert r["qulsi"]["werkzeuge"] == ["camera_capture"]
    assert "fehler" not in antwort(r)
    gegenstaende = json.loads(sql(
        "SELECT json_agg(json_build_object('label', label, 'persoenlich', object_id IS NOT NULL, 'zone', z.name,"
        " 'key', ausschnitt_key)) FROM sightings s LEFT JOIN zones z ON z.id = s.zone_id WHERE s.quelle = 'kamera'"))
    keys = [g for g in gegenstaende if g["label"] == "keys"]
    assert keys and keys[0]["persoenlich"] and keys[0]["zone"] == "Küchentisch"
    cups = [g for g in gegenstaende if g["label"] == "cup"]
    assert cups and not cups[0]["persoenlich"] and cups[0]["zone"] == "Küchentisch"
    # Bildausschnitt liegt im Object Storage, nicht in der Datenbank
    assert (ROOT / "data" / "objects" / keys[0]["key"]).exists()
    bild = httpx.get(f"{CORE}/api/objekt/{keys[0]['key']}", headers=KEY)
    assert bild.status_code == 200 and bild.content[:2] == b"\xff\xd8"


def test_wo_sind_meine_schluessel():
    r = chat("Wo sind meine Schlüssel?")
    assert r["qulsi"]["werkzeuge"] == ["objects_where"]
    text = antwort(r)
    assert "Schlüsselbund" in text and "Küchentisch" in text and "Tasse" in text


def test_ereignis_im_event_bus():
    zeile = sql("SELECT quelle || '|' || text FROM ereignisse WHERE art = 'objekt_bewegt' ORDER BY id DESC LIMIT 1")
    assert zeile == "vision|Schlüsselbund jetzt: Küchentisch"


def test_bluetooth_meldet_neuen_raum():
    subprocess.run(COMPOSE + ["exec", "-T", "mosquitto", "mosquitto_pub", "-t", "espresense/devices/tile_abc/flur",
                              "-m", '{"distance": 1.1}'], cwd=ROOT, check=True)
    warten(lambda: sql("SELECT count(*) FROM sightings WHERE quelle = 'ble'") != "0", 10, "BLE-Sichtung")
    r = chat("Wo ist mein Schlüssel?")
    assert "Schlüsselbund" in antwort(r)
    assert sql("SELECT z.name FROM sightings s JOIN zones z ON z.id = s.zone_id WHERE s.quelle = 'ble'") == "Flur"


def test_privacy_modus_stoppt_kamera():
    sql("UPDATE system_state SET value = 'true' WHERE key = 'privacy_mode'")
    try:
        r = httpx.post(f"{VISION}/snapshot/kueche", timeout=30).json()
        assert "Privacy" in r["fehler"]
    finally:
        sql("UPDATE system_state SET value = 'false' WHERE key = 'privacy_mode'")


# ---------------------------------------------------------------- Memory, Aufgaben, Kalender, Dateien


def test_person_speichern_und_finden():
    r = chat("Max ist mein Kollege, seine Mail ist max@example.org")
    assert r["qulsi"]["werkzeuge"] == ["persons_save"]
    assert sql("SELECT kontakt->>'mail' FROM personen WHERE name = 'Max'") == "max@example.org"


def test_gedaechtnis_in_qdrant_speichern_und_finden():
    chat("Merk dir, dass Max seinen Kaffee schwarz trinkt")
    assert sql("SELECT art FROM erinnerungen WHERE text LIKE '%Kaffee schwarz%'") == "semantisch"
    r = chat("Was weißt du über Max und Kaffee?")
    assert r["qulsi"]["werkzeuge"] == ["memory_search"]
    assert "schwarz" in antwort(r)


def test_aufgabe_anlegen_und_in_heute():
    r = chat("Neue Aufgabe: Angebot Küche prüfen")
    assert r["qulsi"]["werkzeuge"] == ["tasks_create"]
    heute = httpx.get(f"{CORE}/api/heute", headers=KEY).json()
    assert "Angebot Küche prüfen" in [a["titel"] for a in heute["aufgaben"]]


def test_kalender_kommt_ueber_egress_in_den_cache():
    warten(lambda: sql("SELECT count(*) FROM termine WHERE titel = 'Zahnarzt'") == "1", 60, "Kalender-Sync")
    r = chat("Was habe ich heute Nachmittag vor?")
    assert r["qulsi"]["werkzeuge"] == ["calendar_search"]
    assert "Zahnarzt" in antwort(r) and "Teamrunde" in antwort(r)  # Teamrunde ist ein Serientermin
    heute = httpx.get(f"{CORE}/api/heute", headers=KEY).json()
    termine = {t["titel"]: t for t in heute["termine"]}
    assert termine["Zahnarzt"]["start"] == "16:00" and termine["Müll rausbringen"]["ganztags"]


def test_dateien_werden_indiziert_und_belasten():
    warten(lambda: sql("SELECT count(*) FROM dokumente WHERE geaendert IS NOT NULL") == "1", 90, "Datei-Index")
    r = chat("Was steht in meinen Dateien über das Küchenangebot?")
    assert r["qulsi"]["werkzeuge"] == ["files_search"]
    assert "Angebot_Kueche.md" in antwort(r)
    assert r["qulsi"]["belastet"] is True  # Dateiinhalt ist fremder Text
    # memory.search findet die Datei auch (Qdrant-Collection "dokumente")
    assert "Angebot_Kueche.md" in antwort(chat("Was weißt du über das Angebot vom Küchenstudio?"))


# ---------------------------------------------------------------- Policy Engine und Freigaben


def test_mail_entwurf_freigabe_senden():
    r = chat("Schick Max eine Nachricht, dass ich später komme")
    assert r["qulsi"]["werkzeuge"] == ["persons_search", "email_draft", "email_send"]
    assert "Handy" in antwort(r) or "Freigabe" in antwort(r)
    assert httpx.get(f"{FAKE_HA}/test/mails").json() == []  # noch nichts gesendet

    # Handy zeigt den Entwurf, nicht nur die Nummer
    msg = [m for m in ntfy_nachrichten() if m.get("title") == "Freigabe nötig: email.send"][-1]
    assert "An: max@example.org" in msg["message"] and "ich später komme" in msg["message"]

    r = httpx.post(freigabe_link("Freigabe nötig: email.send"), timeout=30)
    assert r.status_code == 200 and r.json()["status"] == "freigegeben", r.text
    mails = httpx.get(f"{FAKE_HA}/test/mails").json()
    assert len(mails) == 1 and "To: max@example.org" in mails[0] and "ich später komme" in mails[0]
    assert sql("SELECT gesendet IS NOT NULL FROM mail_entwuerfe ORDER BY id DESC LIMIT 1") == "t"


def test_haustuer_nur_mit_handy_freigabe():
    vorher = len(ha_aufrufe())
    r = chat("Öffne die Haustür", model="qulsi-stimme")
    assert "Handy" in antwort(r) and "Bitte in der App unter Freigaben bestätigen." in antwort(r)
    assert ha_aufrufe()[vorher:] == []

    ja = freigabe_link("Freigabe nötig: door.unlock")
    falsch = ja.split("token=")[0] + "token=falsch&entscheid=ja"
    assert httpx.post(falsch).status_code == 409
    seite = httpx.get(ja.replace("&entscheid=ja", ""))
    assert seite.status_code == 200 and "lock.haustuer" in seite.text

    r = httpx.post(ja, timeout=30)
    assert r.status_code == 200 and r.json()["status"] == "freigegeben"
    assert ha_aufrufe()[vorher:] == [{"domain": "lock", "service": "unlock", "body": {"entity_id": "lock.haustuer"}}]
    assert httpx.post(ja).status_code == 409  # Einmal-Token
    assert sql("SELECT connector FROM audit_log WHERE tool = 'door.unlock' AND entscheid = 'ausgefuehrt_nach_freigabe'") \
        == "home_assistant"
    offen = httpx.get(f"{CORE}/api/freigaben", headers=KEY).json()
    assert any(f["tool"] == "door.unlock" and f["status"] == "freigegeben" for f in offen)


def test_web_inhalt_sperrt_kritische_aktionen_im_selben_gespraech():
    r1 = chat("Suche im Web nach Öffnungszeiten Wertstoffhof")
    assert r1["qulsi"]["werkzeuge"] == ["browser_search"] and r1["qulsi"]["belastet"] is True
    verlauf = [{"role": "user", "content": "Suche im Web nach Öffnungszeiten Wertstoffhof"},
               {"role": "assistant", "content": antwort(r1)}]
    vorher = len(ha_aufrufe())
    r2 = chat("Öffne die Haustür", verlauf=verlauf)
    assert r2["qulsi"]["belastet"] is True
    assert ha_aufrufe()[vorher:] == []
    assert sql("SELECT entscheid FROM audit_log WHERE tool = 'door.unlock' ORDER BY zeit DESC LIMIT 1") == "abgelehnt"


def tuer_freigabe() -> dict:
    """Haustür anfragen und die neue offene Freigabe aus /api/freigaben holen."""
    chat("Öffne die Haustür", model="qulsi-stimme")
    f = httpx.get(f"{CORE}/api/freigaben", headers=KEY).json()[0]
    assert f["tool"] == "door.unlock" and f["status"] == "offen", f
    return f


def app_entscheiden(fid: str, entscheidung: str, pin: str | None = None, headers=KEY) -> httpx.Response:
    return httpx.post(f"{CORE}/api/freigaben/{fid}", json={"entscheidung": entscheidung, "pin": pin},
                      headers=headers, timeout=30)


def test_freigabe_in_der_app_nur_mit_pin():
    assert httpx.get(f"{CORE}/api/freigaben/pin", headers=KEY).json() == {"eingerichtet": True, "gesperrt_bis": None,
                                                                         "einrichtungscode_offen_bis": None}
    vorher = len(ha_aufrufe())
    f = tuer_freigabe()
    assert {"vorschau", "begruendung", "laeuft_ab_iso", "belastet"} <= set(f)
    assert app_entscheiden(f["id"], "erlauben", "246810", headers={}).status_code == 401
    r = app_entscheiden(f["id"], "erlauben", "111111")
    assert r.status_code == 403 and r.json()["versuche_uebrig"] == 4
    assert app_entscheiden(f["id"], "erlauben").status_code == 403
    assert ha_aufrufe()[vorher:] == []
    r = app_entscheiden(f["id"], "erlauben", "246810")
    assert r.status_code == 200 and r.json()["status"] == "freigegeben", r.text
    assert ha_aufrufe()[vorher:] == [{"domain": "lock", "service": "unlock", "body": {"entity_id": "lock.haustuer"}}]
    assert app_entscheiden(f["id"], "erlauben", "246810").status_code == 409  # nur einmal
    entscheide = sql(f"SELECT entscheid FROM audit_log WHERE tool = 'door.unlock' AND entscheid <> 'bestaetigung' AND zeit > "
                     f"(SELECT erstellt FROM confirmations WHERE id = '{f['id']}') ORDER BY id").split()
    assert entscheide == ["app_pin_falsch", "app_pin_falsch", "app_erlaubt", "ausgefuehrt_nach_freigabe"]
    assert any(m.get("title") == "Erledigt: door.unlock" for m in ntfy_nachrichten())  # Handy erfährt es

    # Ablehnen geht ohne PIN
    f = tuer_freigabe()
    r = app_entscheiden(f["id"], "ablehnen")
    assert r.json() == {"status": "abgelehnt"}
    assert sql(f"SELECT status FROM confirmations WHERE id = '{f['id']}'") == "abgelehnt"

    # Abgelaufen: weder in der App noch übers Handy
    f = tuer_freigabe()
    sql(f"UPDATE confirmations SET laeuft_ab = now() - interval '1 second' WHERE id = '{f['id']}'")
    assert app_entscheiden(f["id"], "erlauben", "246810").status_code == 409
    assert next(x for x in httpx.get(f"{CORE}/api/freigaben", headers=KEY).json() if x["id"] == f["id"])["status"] \
        == "abgelaufen"
    assert ha_aufrufe()[vorher + 1:] == []


def test_falsche_pins_sperren_die_app_freigabe():
    f = tuer_freigabe()
    try:
        codes = [app_entscheiden(f["id"], "erlauben", "000000").status_code for _ in range(5)]
        assert codes == [403, 403, 403, 403, 429]
        r = app_entscheiden(f["id"], "erlauben", "246810")
        assert r.status_code == 429 and r.json()["gesperrt_bis"]
        assert httpx.get(f"{CORE}/api/freigaben/pin", headers=KEY).json()["gesperrt_bis"]
        assert any(m.get("title") == "Freigabe-PIN gesperrt" for m in ntfy_nachrichten())
        # Das Handy geht weiterhin
        assert httpx.post(freigabe_link("Freigabe nötig: door.unlock"), timeout=30).json()["status"] == "freigegeben"
    finally:
        sql("DELETE FROM system_state WHERE key = 'freigabe_pin'")


def probe_wav(sekunden: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\0\1" * int(sekunden * 24000))
    return buf.getvalue()


def test_stimmprobe_erst_nach_freigabe():
    stimme = ROOT / "data" / "test-stimme"
    st = httpx.get(f"{CORE}/api/stimme/probe", headers=KEY).json()
    assert st["klonen"] is True and st["vorhanden"] is False and st["offen"] is None, st
    zu_kurz = httpx.post(f"{CORE}/api/stimme/probe", headers=KEY, data={"text": "Hallo"},
                         files={"audio": ("p.wav", probe_wav(2), "audio/wav")})
    assert zu_kurz.status_code == 422
    r = httpx.post(f"{CORE}/api/stimme/probe", headers=KEY, data={"text": "Hallo, ich bin Anna."},
                   files={"audio": ("p.wav", probe_wav(7), "audio/wav")}, timeout=30)
    assert r.status_code == 200 and r.json()["status"] == "freigabe_noetig", r.text
    fid = r.json()["freigabe"]
    assert not (stimme / "stimme.wav").exists()  # noch nicht übernommen
    msg = [m for m in ntfy_nachrichten() if m.get("title") == "Freigabe nötig: stimme.probe"][-1]
    assert "Hallo, ich bin Anna." in msg["message"]
    assert httpx.get(f"{CORE}/api/stimme/probe", headers=KEY).json()["offen"] == fid
    assert app_entscheiden(fid, "erlauben", "000000").status_code == 403
    r = app_entscheiden(fid, "erlauben", "246810")
    assert r.status_code == 200 and r.json()["status"] == "freigegeben", r.text
    assert (stimme / "stimme.txt").read_text(encoding="utf-8") == "Hallo, ich bin Anna."
    assert (stimme / "stimme.wav").stat().st_size > 300000
    assert httpx.get(f"{CORE}/api/stimme/probe", headers=KEY).json()["vorhanden"] is True
    assert sql("SELECT entscheid FROM audit_log WHERE tool = 'stimme.probe' ORDER BY id DESC LIMIT 1") \
        == "ausgefuehrt_nach_freigabe"
    # Abgelehnt übers Handy: die Probe wird gelöscht, die Stimme bleibt
    httpx.post(f"{CORE}/api/stimme/probe", headers=KEY, data={"text": "Fremde Stimme"},
               files={"audio": ("p.wav", probe_wav(6), "audio/wav")}, timeout=30).raise_for_status()
    msg = [m for m in ntfy_nachrichten() if m.get("title") == "Freigabe nötig: stimme.probe"][-1]
    nein = next(a for a in msg["actions"] if a["label"] == "Ablehnen")["url"]
    assert httpx.post(nein, timeout=30).json()["status"] == "abgelehnt"
    assert list((stimme / "ausstehend").iterdir()) == []
    assert (stimme / "stimme.txt").read_text(encoding="utf-8") == "Hallo, ich bin Anna."
    # Sprachausgabe läuft weiter
    assert httpx.post(f"{CORE}/api/tts", headers=KEY, json={"text": "Test."}, timeout=60).status_code == 200


def test_stimme_in_der_app_waehlen():
    """Eigene Stimme oder eingebauter Sprecher, als Auswahl und ohne Neustart; die Probe bleibt erhalten."""
    stimme = ROOT / "data" / "test-stimme"
    s = httpx.get(f"{CORE}/api/einstellungen", headers=KEY).json()["stimme"]
    assert [a["id"] for a in s["auswahl"]] == ["eigene", "ryan", "vivian"], s
    assert s["aktiv"] == "eigene" and s["eigene_vorhanden"] is True
    assert httpx.post(f"{CORE}/api/einstellungen/stimme", headers=KEY, json={"stimme": "Ryan!"}).status_code == 400
    httpx.post(f"{CORE}/api/einstellungen/stimme", headers=KEY, json={"stimme": "ryan"}).raise_for_status()
    assert httpx.get(f"{CORE}/api/einstellungen", headers=KEY).json()["stimme"]["aktiv"] == "ryan"
    assert httpx.post(f"{CORE}/api/tts", headers=KEY, json={"text": "Test."}, timeout=60).status_code == 200
    assert (stimme / "stimme.wav").exists()  # eigene Probe bleibt
    httpx.post(f"{CORE}/api/einstellungen/stimme", headers=KEY, json={"stimme": "eigene"}).raise_for_status()
    assert httpx.get(f"{CORE}/api/einstellungen", headers=KEY).json()["stimme"]["aktiv"] == "eigene"


def test_umgebung_box():
    u = httpx.get(f"{CORE}/api/umgebung", headers=KEY).json()
    assert u["laufzeit"] == "box" and u["tresor"] is True and u["warnung"] is None
    assert "tresor setzen" in u["befehle"]["nas"]


def test_neues_gespraech_ohne_fremden_inhalt():
    """Die Markierung hängt am mitgeschickten Verlauf: ohne ihn (Neues Gespräch) ist sie weg."""
    r1 = chat("Suche im Web nach Öffnungszeiten Wertstoffhof")
    verlauf = [{"role": "user", "content": "Suche im Web nach Öffnungszeiten Wertstoffhof"},
               {"role": "assistant", "content": antwort(r1)}]
    assert chat("Wie spät ist es?", verlauf=verlauf)["qulsi"]["belastet"] is True
    assert chat("Wie spät ist es?")["qulsi"]["belastet"] is False


# ---------------------------------------------------------------- Sicherheitszonen


def test_egress_laesst_nur_manifest_ziele_durch():
    # Der Web-Connector darf laut Manifest nirgends hin
    p = im_container("connector-web", "import httpx; print(httpx.get('http://fake-ha:8123/api/states', timeout=5).status_code)")
    assert p.stdout.strip() == "403", p.stderr
    # ...und ohne Proxy gibt es keinen Weg nach draußen
    p = im_container("connector-web", "import httpx; httpx.get('http://fake-ha:8123/', timeout=5, trust_env=False)")
    assert p.returncode != 0
    # Der Kalender-Connector darf zu seinem Kalender, aber auf keinem anderen Port
    p = im_container("connector-calendar",
                     "import httpx; print(httpx.get('http://fake-ha:8123/kalender.ics', timeout=5).status_code,"
                     " httpx.get('http://fake-ha:2525/', timeout=5).status_code)")
    assert p.stdout.split() == ["200", "403"], p.stdout + p.stderr
    logs = subprocess.run(COMPOSE + ["logs", "egress"], cwd=ROOT, capture_output=True, text=True).stdout
    assert "VERWEIGERT connector=web" in logs


def test_core_und_modelle_haben_kein_internet():
    p = im_container("core", "import httpx; httpx.get('http://fake-ha:8123/', timeout=5, trust_env=False)")
    assert p.returncode != 0
    p = im_container("vision", "import socket; socket.create_connection(('fake-ha', 8123), timeout=5)")
    assert p.returncode != 0


EIGENE = ["core", "egress", "vision", "voice", "tts", "fake-llm", "tresor", "connector-calendar", "connector-mail", "connector-files"]


@pytest.mark.parametrize("dienst", EIGENE)
def test_eigene_container_gehaertet(dienst):
    cid = subprocess.run(COMPOSE + ["ps", "-q", dienst], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    info = json.loads(subprocess.run(["docker", "inspect", cid], capture_output=True, text=True).stdout)[0]
    host = info["HostConfig"]
    assert info["Config"]["User"] == "10001"  # nicht root
    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"] and not host["CapAdd"]
    assert "no-new-privileges:true" in host["SecurityOpt"]
    # Seccomp-Standardprofil aktiv (nicht "unconfined"); status zeigt es im laufenden Prozess
    status = im_container(dienst, "print(open('/proc/self/status').read())").stdout
    assert "Seccomp:\t2" in status and "CapEff:\t0000000000000000" in status
    schreiben = im_container(dienst, "open('/app-test', 'w')")
    assert schreiben.returncode != 0


# ---------------------------------------------------------------- Voice, Event Bus, Schnittstellen


def test_stimme_rundweg():
    r = httpx.post(f"{CORE}/api/stimme", headers=KEY, files={"audio": ("a.webm", "Wie spät ist es?".encode())}, timeout=60)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["text"] == "Wie spät ist es?" and d["antwort"].startswith("Es ist ")
    audio = httpx.post(f"{CORE}/api/tts", headers=KEY, json={"text": d["antwort"]}, timeout=60)
    assert audio.status_code == 200 and audio.content[:4] == b"RIFF"
    # Die Antwort kommt vom TTS-Dienst (24 kHz), nicht vom Piper-Ersatz im Voice-Dienst (16 kHz)
    with wave.open(io.BytesIO(audio.content)) as wf:
        assert wf.getframerate() == 24000


def test_event_bus_live():
    # Kein Schlüssel in der URL: der Browser holt sich ein Sitzungs-Cookie und öffnet damit EventSource
    assert httpx.get(f"{CORE}/api/events", params={"key": "qulsi-test"}, timeout=10).status_code == 401
    r = httpx.post(f"{CORE}/api/sitzung", headers=KEY)
    cookie = r.cookies["qulsi_sitzung"]
    kopf = r.headers["set-cookie"].lower()
    assert "httponly" in kopf and "samesite=strict" in kopf and "path=/api" in kopf
    # Das Cookie öffnet nur lesende Wege, keine Änderungen
    assert httpx.post(f"{CORE}/api/chat", cookies={"qulsi_sitzung": cookie}, json={"nachricht": "x"}).status_code == 401
    gesehen: list[str] = []
    with httpx.stream("GET", f"{CORE}/api/events", cookies={"qulsi_sitzung": cookie}, timeout=30) as s:
        zeilen = s.iter_lines()
        assert next(zeilen).startswith(": verbunden")
        httpx.post(f"{CORE}/api/chat", headers=KEY, json={"nachricht": "Neue Aufgabe: Fenster putzen"}, timeout=60)
        for z in zeilen:
            if z.startswith("data: "):
                gesehen.append(z)
                if "Fenster putzen" in z:
                    break
    assert any("Aufgabe angelegt: Fenster putzen" in g for g in gesehen)
    httpx.delete(f"{CORE}/api/sitzung", cookies={"qulsi_sitzung": cookie})
    # Das alte Einmal-Token gibt es nicht mehr: nur Cookie oder Header
    assert httpx.post(f"{CORE}/api/events/token", headers=KEY).status_code in (404, 405)
    assert httpx.get(f"{CORE}/api/events", params={"t": "x"}, timeout=10).status_code == 401
    assert httpx.get(f"{CORE}/api/events", cookies={"qulsi_sitzung": cookie}, timeout=10).status_code == 401


def test_keine_schluessel_im_log():
    httpx.get(f"{CORE}/api/heute", params={"key": "qulsi-test", "token": "geheim123"}, timeout=10)
    logs = subprocess.run(COMPOSE + ["logs", "core"], cwd=ROOT, capture_output=True, text=True).stdout
    assert "key=***" in logs
    assert "qulsi-test" not in logs and "geheim123" not in logs
    # Auch die Einmal-Tokens der Freigabe-Links stehen nicht im Log
    assert "/bestaetigung/" in logs and not [z for z in logs.splitlines() if "token=" in z and "token=***" not in z]


def test_app_chat_und_timeline():
    r = httpx.post(f"{CORE}/api/chat", headers=KEY, json={"nachricht": "Welche Aufgaben habe ich?"}, timeout=60).json()
    assert r["werkzeuge"] == ["tasks_list"] and "Fenster putzen" in r["antwort"]
    timeline = httpx.get(f"{CORE}/api/timeline", headers=KEY).json()
    texte = " ".join(t["text"] for t in timeline)
    assert "Schlüsselbund jetzt" in texte and "Zahnarzt" in texte


def test_litellm_schaltet_denkmodus_ab():
    p = im_container("fake-llm", "import urllib.request, json; print(urllib.request.urlopen('http://localhost:8080/debug/anfragen').read().decode())")
    anfragen = json.loads(p.stdout)
    mit_tools = [a for a in anfragen if a.get("tools") and a.get("model") == "bonsai"]
    assert mit_tools, "keine Anfrage mit Werkzeugen gesehen"
    a = mit_tools[-1]
    assert a["chat_template_kwargs"] == {"enable_thinking": False}
    assert a["top_k"] == 20


def test_streaming_fuer_open_webui():
    with httpx.stream("POST", f"{CORE}/v1/chat/completions", headers=KEY, timeout=60,
                      json={"model": "qulsi", "stream": True,
                            "messages": [{"role": "user", "content": "Wo sind meine Schlüssel?"}]}) as r:
        zeilen = [z for z in r.iter_lines() if z.startswith("data: ")]
    assert zeilen[-1] == "data: [DONE]"
    text = "".join(json.loads(z[6:])["choices"][0]["delta"].get("content", "") for z in zeilen[:-1])
    assert "Schlüsselbund" in text


# ---------------------------------------------------------------- Offline-Modus (zuletzt, dauert ca. 1 Minute)


def test_offline_modus():
    subprocess.run(COMPOSE + ["stop", "fake-ha"], cwd=ROOT, check=True, capture_output=True)
    try:
        warten(lambda: httpx.get(f"{CORE}/health").json()["offline"] is True, 90, "Offline erkannt")
        status = httpx.get(f"{CORE}/api/status", headers=KEY).json()
        assert status["werkzeuge"]["browser.search"]["offline"] is False
        # Websuche wird dem Modell nicht mehr angeboten ...
        r = chat("Suche im Web nach Wetter")
        assert r["qulsi"]["werkzeuge"] == [] and r["qulsi"]["offline"] is True
        # ... Kalender (lokaler Cache), Aufgaben und Gedächtnis gehen weiter
        r = chat("Was habe ich heute vor?")
        assert r["qulsi"]["werkzeuge"] == ["calendar_search"] and "Zahnarzt" in antwort(r)
        assert chat("Welche Aufgaben habe ich?")["qulsi"]["werkzeuge"] == ["tasks_list"]
        assert "Offline" in sql("SELECT text FROM ereignisse WHERE art = 'offline' ORDER BY id DESC LIMIT 1")
    finally:
        subprocess.run(COMPOSE + ["start", "fake-ha"], cwd=ROOT, check=True, capture_output=True)
    warten(lambda: httpx.get(f"{CORE}/health").json()["offline"] is False, 90, "wieder online")


# ---------------------------------------------------------------- Connector Hub: Absicherung


def connector(name: str) -> dict:
    r = httpx.get(f"{CORE}/api/connectoren", headers=KEY, timeout=30)
    r.raise_for_status()
    return next(c for c in r.json() if c["name"] == name)


def test_connectoren_ohne_dns_nach_draussen():
    p = im_container("connector-web", "import socket; socket.gethostbyname('example.com')")
    assert p.returncode != 0 and "resolution" in p.stderr


def test_connector_ressourcen_limits():
    cid = subprocess.run(COMPOSE + ["ps", "-q", "connector-mail"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    host = json.loads(subprocess.run(["docker", "inspect", cid], capture_output=True, text=True).stdout)[0]["HostConfig"]
    assert host["Memory"] == 512 * 1024 * 1024 and host["PidsLimit"] == 256 and host["NanoCpus"] == 1_000_000_000


def test_signatur_geprueft_und_alle_connectoren_frei():
    assert (ROOT / "connectors" / "SIGNATUR.json").exists()
    assert connector("mail")["signatur"] is None and connector("calendar")["signatur"] is None


def test_modus_nur_lesen_und_aus():
    try:
        httpx.post(f"{CORE}/api/connectoren/mail", headers=KEY, json={"modus": "nur_lesen"}).raise_for_status()
        m = connector("mail")
        assert "email.search" in m["angeboten"] and "email.send" not in m["angeboten"] and "email.draft" not in m["angeboten"]
        httpx.post(f"{CORE}/api/connectoren/mail", headers=KEY, json={"modus": "aus"}).raise_for_status()
        assert connector("mail")["angeboten"] == []
        vorher = sql("SELECT count(*) FROM mail_entwuerfe")
        chat("Schreib Max eine Mail, dass ich später komme")
        assert sql("SELECT count(*) FROM mail_entwuerfe") == vorher  # kein Entwurf, Connector ist aus
    finally:
        httpx.post(f"{CORE}/api/connectoren/mail", headers=KEY, json={"modus": "an"}).raise_for_status()
    assert "email.send" in connector("mail")["angeboten"]


def test_tresor_gibt_geheimnisse_nur_laut_manifest():
    code = ("import httpx; print(httpx.get('http://tresor:8400/geheimnis/{}', timeout=5, trust_env=False).status_code)")
    assert im_container("connector-web", code.format("HA_TOKEN")).stdout.strip() == "403"
    assert im_container("connector-home-assistant", code.format("HA_TOKEN")).stdout.strip() == "200"
    assert "HA_TOKEN" not in im_container("connector-home-assistant", "import os; print(sorted(os.environ))").stdout


def test_egress_protokoll_und_quarantaene():
    # Der Web-Connector versucht es zehnmal bei einem verbotenen Ziel
    im_container("connector-web", "import httpx\nfor _ in range(10):\n    httpx.get('http://fake-ha:8123/', timeout=5)")
    try:
        for _ in range(20):
            if connector("web")["quarantaene"]:
                break
            time.sleep(1)
        assert "abgelehnte Verbindungen" in connector("web")["quarantaene"]["grund"]
        p = im_container("connector-web", "import httpx; print(httpx.get('http://fake-ha:8123/', timeout=5).text)")
        assert "Quarantaene" in p.stdout
        assert any("Quarantäne" in e["text"] for e in httpx.get(f"{CORE}/api/timeline", headers=KEY).json())
    finally:
        r = httpx.post(f"{CORE}/api/connectoren/web/freigeben", headers=KEY, timeout=10)
    assert r.json()["freigegeben"] is True and connector("web")["quarantaene"] is None
    zeilen = (ROOT / "data" / "egress" / "egress.jsonl").read_text().splitlines()
    assert any('"connector": "web"' in z and '"verweigert"' in z for z in zeilen)


# ---------------------------------------------------------------- Nutzername und persönliche Daten


def _letzter_systemprompt(frage: str = "Wer bin ich?") -> str:
    p = im_container("fake-llm", "import urllib.request; print(urllib.request.urlopen('http://localhost:8080/debug/anfragen').read().decode())")
    a = next(a for a in reversed(json.loads(p.stdout)) if a["messages"][-1].get("content") == frage)
    return next(m["content"] for m in a["messages"] if m["role"] == "system")


def test_kein_fest_eingebauter_name():
    assert httpx.get(f"{CORE}/api/nutzer", headers=KEY).json() == {"name": None, "bundesland": None}
    # „Wie heiße ich?“ beantwortet QuLSI selbst aus den Einstellungen, ohne Modell (pruefen auf dem Pod)
    assert "kenne ich noch nicht" in chat("Wie heiße ich?")["choices"][0]["message"]["content"]
    chat("Wer bin ich?")
    prompt = _letzter_systemprompt()
    assert "Anna" not in prompt and "weißt du nicht" in prompt and "Erfinde nie, woher du etwas weißt" in prompt
    try:
        assert httpx.post(f"{CORE}/api/nutzer", headers=KEY, json={"name": "  Kim  "}).json() == {"name": "Kim", "bundesland": None}
        assert chat("Wie heiße ich?")["choices"][0]["message"]["content"] == "Du heißt Kim."
        chat("Wer bin ich?")
        assert "Dein Nutzer heißt Kim" in _letzter_systemprompt()
        # Jede Box steht woanders: Bundesland aus der App oder dem Gespräch, nur gesendete Felder ändern sich
        assert httpx.post(f"{CORE}/api/nutzer", headers=KEY, json={"bundesland": "BY"}).json() == {"name": "Kim", "bundesland": "BY"}
        assert httpx.post(f"{CORE}/api/nutzer", headers=KEY, json={"bundesland": "Atlantis"}).status_code == 422
        r = chat("Ich wohne in Sachsen.")
        assert r["qulsi"]["werkzeuge"] == ["user_region_set"]
        assert httpx.get(f"{CORE}/api/nutzer", headers=KEY).json()["bundesland"] == "SN"
    finally:
        httpx.post(f"{CORE}/api/nutzer", headers=KEY, json={"name": None, "bundesland": None}).raise_for_status()
    assert httpx.get(f"{CORE}/api/nutzer").status_code == 401


def test_daten_suchen_und_loeschen():
    sql("INSERT INTO erinnerungen (art, text) VALUES ('praeferenz', 'Testeintrag Zitronentee')")
    zr = COMPOSE + ["exec", "-T", "core", "python", "-m", "qulsi_core.zuruecksetzen"]
    zeigen = subprocess.run(zr + ["zeigen"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    assert "Testeintrag Zitronentee" in zeigen and "Nutzername: (keiner)" in zeigen
    ohne_ja = subprocess.run(zr + ["suchen", "Zitronentee"], cwd=ROOT, capture_output=True, text=True, input="nein\n")
    assert ohne_ja.returncode == 1 and sql("SELECT count(*) FROM erinnerungen WHERE text LIKE '%Zitronentee%'") == "1"
    out = subprocess.run(zr + ["suchen", "Zitronentee", "--ja"], cwd=ROOT, capture_output=True, text=True, check=True)
    assert "1 Einträge gelöscht" in out.stdout
    assert sql("SELECT count(*) FROM erinnerungen WHERE text LIKE '%Zitronentee%'") == "0"


def test_person_umbenennen():
    code = """
import asyncio, os
from qulsi_core.memory import Graph, Store
from qulsi_core.tools import Kern
async def main():
    s = Store(os.environ["DATABASE_URL"]); k = Kern(s, None, Graph(s))
    await k.persons_save("Testperson Alt", notizen="umbenennen")
    await k.graph.verbinden("person", "Testperson Alt", "kennt", "person", "Anna")
    print(await k.persons_rename("testperson alt", "Testperson Neu"))
    print(await k.persons_rename("Gibtsnicht", "X"))
    p = await s.eine("SELECT name, aliase FROM personen WHERE name = 'Testperson Neu'")
    b = await s.alle("SELECT von FROM beziehungen WHERE von LIKE 'Testperson%'")
    print(p["aliase"], [x["von"] for x in b])
asyncio.run(main())
"""
    p = im_container("core", code)
    assert p.returncode == 0, p.stderr
    zeilen = p.stdout.strip().splitlines()
    assert zeilen[0] == "Testperson Alt heißt jetzt Testperson Neu."
    assert zeilen[1] == "Eine Person Gibtsnicht gibt es nicht."
    assert zeilen[2] == "['Testperson Alt'] ['Testperson Neu']"
    sql("DELETE FROM personen WHERE name = 'Testperson Neu'; DELETE FROM beziehungen WHERE von = 'Testperson Neu'")



def test_abnahme_pruefen_laeuft_und_raeumt_auf():
    """qulsi.sh pruefen (hier mit dem Test-Modell): alle Fälle OK, danach ist nichts davon gespeichert."""
    vorher = sql("SELECT count(*) FROM erinnerungen") + "/" + sql("SELECT count(*) FROM termine")
    httpx.post(f"{CORE}/api/nutzer", headers=KEY, json={"name": "Kim"}).raise_for_status()
    try:
        p = subprocess.run(COMPOSE + ["exec", "-T", "core", "python", "-m", "qulsi_core.abnahme", "--teststack"],
                           cwd=ROOT, capture_output=True, text=True, timeout=600)
        assert p.returncode == 0, p.stdout + p.stderr
        assert "16 von 16 OK" in p.stdout and "Nutzername: Kim" in p.stdout
        assert sql("SELECT count(*) FROM erinnerungen WHERE text ILIKE '%Zitronentee%'") == "0"
        assert sql("SELECT count(*) FROM termine WHERE titel = 'Arzttermin'") == "0"
        assert sql("SELECT count(*) FROM erinnerungen") + "/" + sql("SELECT count(*) FROM termine") == vorher
        assert httpx.get(f"{CORE}/api/nutzer", headers=KEY).json()["name"] == "Kim"
    finally:
        httpx.post(f"{CORE}/api/nutzer", headers=KEY, json={"name": None})


def test_fruehere_gespraeche_bleiben_auf_der_box():
    chat("Was weißt du über das Angebot vom Küchenstudio?")
    r = chat("Worüber haben wir gesprochen?")
    assert r["qulsi"]["werkzeuge"] == ["conversations_search"]
    assert sql("SELECT count(*) FROM gespraeche WHERE frage ILIKE '%Küchenstudio%'") != "0"


def test_timeline_mit_inhalt_gruppe_und_schluessel():
    chat("Merk dir, dass die Gießkanne im Keller steht.")
    heute = httpx.get(f"{CORE}/api/gedaechtnis/timeline", headers=KEY).json()
    gemerkt = [e for e in heute if e["text"].startswith("Gemerkt: ") and "Gießkanne" in e["text"]]
    assert gemerkt and gemerkt[-1]["werkzeug"] == "memory.remember" and gemerkt[-1]["gruppe"] == "inhalt"
    notiz = [e for e in heute if e["schluessel"] == gemerkt[-1]["schluessel"]]
    assert len(notiz) == 1  # semantische Erinnerung steht nicht extra in der Timeline, nur die Aktion
    assert all(e["datum"] == heute[-1]["datum"] for e in notiz)
    assert not [e for e in heute if e["text"].startswith("calendar.create")]
    assert {e["gruppe"] for e in heute if e["art"] == "freigabe"} <= {"protokoll"}


# ---------------------------------------------------------------- Hochladen aus der App


def _jpeg() -> bytes:
    import io

    from PIL import Image

    puffer = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 120, 40)).save(puffer, "JPEG")
    return puffer.getvalue()


def hochladen(name: str, daten: bytes, mime: str) -> httpx.Response:
    return httpx.post(f"{CORE}/api/hochladen", headers=KEY, files={"datei": (name, daten, mime)}, timeout=60)


def test_hochladen_foto_und_dokument():
    g = httpx.get(f"{CORE}/api/hochladen", headers=KEY).json()
    assert g["grenzen"]["max_mb"] == 25 and "application/pdf" in g["grenzen"]["typen"]
    assert httpx.get(f"{CORE}/api/hochladen").status_code == 401

    # Prüfung im Core: Endung, MIME und Inhalt müssen passen
    assert hochladen("tool.exe", b"MZ\x90\x00", "application/octet-stream").status_code == 415
    falsch = hochladen("rechnung.pdf", b"kein pdf", "application/pdf")
    assert falsch.status_code == 400 and "PDF" in falsch.json()["fehler"]

    foto = hochladen("../../urlaub.jpg", _jpeg(), "image/jpeg")
    assert foto.status_code == 200, foto.text
    f = foto.json()
    assert f["art"] == "foto" and f["name"] == "urlaub.jpg" and f["status"] == "eingeordnet" and f["vorschau"]
    assert httpx.get(f"{CORE}{f['vorschau']}", headers=KEY).content[:3] == b"\xff\xd8\xff"
    assert list((ROOT / "data" / "fotos" / "Hochgeladen").rglob(f"{f['id']}.jpg"))
    assert sql(f"SELECT count(*) FROM fotos WHERE quelle LIKE 'foto:Hochgeladen/%/{f['id']}.jpg'") == "1"
    assert sql("SELECT count(*) FROM ereignisse WHERE art = 'datei' AND text = 'Foto hochgeladen: urlaub.jpg'") == "1"

    doc = hochladen("Notiz Garten.md", "Die Gartenschere liegt im blauen Schuppen.".encode(), "text/markdown")
    assert doc.status_code == 200, doc.text
    d = doc.json()
    assert d["art"] == "dokument" and d["status"] in ("wartet", "eingeordnet")
    warten(lambda: httpx.get(f"{CORE}/api/hochladen", headers=KEY).json()["dateien"][0]["status"] == "eingeordnet",
           90, "Dokument indiziert")
    assert [x["id"] for x in httpx.get(f"{CORE}/api/hochladen?limit=2", headers=KEY).json()["dateien"]] == [d["id"], f["id"]]

    # Im Gespräch: Dokument wird mit files_read gelesen, das Gespräch gilt als belastet (fremd)
    r = httpx.post(f"{CORE}/api/chat", headers=KEY, timeout=120,
                   json={"nachricht": "Schau dir bitte die Datei an.", "anhaenge": [d["id"]]}).json()
    assert r["werkzeuge"] == ["files_read"] and "Gartenschere" in r["antwort"] and r["belastet"] is True
    # Foto ohne Bildmodell (Teststack ohne mmproj): ehrlich, und ebenfalls belastet
    r = httpx.post(f"{CORE}/api/chat", headers=KEY, timeout=120,
                   json={"nachricht": "Was ist das?", "anhaenge": [f["id"], "h_gibtsnicht"]}).json()
    assert r["route"] == "system" and "Bilder kann ich" in r["antwort"] and r["belastet"] is True

    # Löschen: Datei, Foto-Eintrag und Liste
    assert httpx.delete(f"{CORE}/api/hochladen/{f['id']}", headers=KEY).status_code == 200
    assert httpx.delete(f"{CORE}/api/hochladen/{d['id']}", headers=KEY).status_code == 200
    assert httpx.delete(f"{CORE}/api/hochladen/../etc", headers=KEY).status_code == 404
    assert sql(f"SELECT count(*) FROM fotos WHERE quelle LIKE '%{f['id']}%'") == "0"
    assert not list((ROOT / "data" / "test-dateien" / "Hochgeladen").rglob(f"{d['id']}*"))
    assert httpx.get(f"{CORE}/api/hochladen", headers=KEY).json()["dateien"] == []
