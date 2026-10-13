"""Ende-zu-Ende: Google, Microsoft, NAS und Browser im laufenden Teststack (scripts/test-stack.sh).

Ohne echte Konten: geprüft wird, dass die Connectoren laufen, ihre Werkzeuge mit den richtigen
Stufen im Hub stehen und der Egress-Proxy nur ihre Ziele durchlässt.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
CORE = "http://localhost:8100"
KEY = {"Authorization": "Bearer qulsi-test"}
COMPOSE = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.dev.yml",
           "-f", "tests/integration/docker-compose.test.yml"]


def im_container(dienst: str, code: str) -> subprocess.CompletedProcess:
    return subprocess.run(COMPOSE + ["exec", "-T", dienst, "python", "-c", code], cwd=ROOT, capture_output=True, text=True)


def werkzeug(dienst: str, aufruf: str) -> str:
    """Werkzeug direkt im Connector-Container aufrufen (ohne Sprachmodell)."""
    ordner = dienst.removeprefix("connector-")
    p = im_container(dienst, f"import asyncio, sys; sys.path.insert(0, '/app/{ordner}'); import server; "
                             f"print(asyncio.run(server.{aufruf}))")
    assert p.returncode == 0, p.stderr
    return p.stdout


def test_neue_connectoren_geladen():
    h = httpx.get(f"{CORE}/health").json()
    for c in ("google", "microsoft", "nas", "browser"):
        assert h["connectoren"].get(c) is True, h["connectoren"]
    for f in ("google.mail.send", "google.calendar.create", "google.drive.read", "microsoft.mail.send",
              "microsoft.onedrive.search", "nas.read", "nas.search", "browser.open"):
        assert f in h["werkzeuge"], f
    assert "google_mail_vorschau" not in h["werkzeuge"]


def test_browser_liest_seite_ueber_egress():
    out = werkzeug("connector-browser", "browser_open('http://fake-ha:8123/kalender.ics')")
    assert "BEGIN:VCALENDAR" in out or "VEVENT" in out, out


def test_browser_kommt_nicht_ins_heimnetz():
    # "*" erlaubt jede öffentliche Seite, aber nicht Dienste im Docker-Netz oder Heimnetz
    out = werkzeug("connector-browser", "browser_open('http://ntfy/')")
    assert out.startswith("Fehler") and "öffentlichen Internet" in out, out
    out = werkzeug("connector-browser", "browser_open('http://192.168.178.1/')")
    assert "Heimnetz" in out, out
    logs = subprocess.run(COMPOSE + ["logs", "egress"], cwd=ROOT, capture_output=True, text=True).stdout
    assert "VERWEIGERT connector=browser" in logs


def test_konten_ohne_anmeldung_sagen_es_klar():
    assert "nicht verbunden" in werkzeug("connector-google", "google_mail_search('x')")
    assert "nicht verbunden" in werkzeug("connector-microsoft", "microsoft_mail_search('x')")
    assert "nicht eingerichtet" in werkzeug("connector-nas", "nas_list('')")


def test_google_darf_nur_zu_google():
    p = im_container("connector-google", "import httpx; print(httpx.get('http://fake-ha:8123/', timeout=5).status_code)")
    assert p.stdout.strip() == "403", p.stdout + p.stderr



def test_stufen_aus_den_manifesten():
    w = httpx.get(f"{CORE}/api/status", headers=KEY, timeout=30).json()["werkzeuge"]
    assert w["google.mail.send"]["stufe"] == "high" and w["microsoft.mail.send"]["stufe"] == "high"
    assert w["google.mail.draft"]["stufe"] == "medium" and w["google.calendar.create"]["stufe"] == "medium"
    assert w["nas.read"]["stufe"] == "low" and w["nas.read"]["offline"] is True
    assert w["browser.open"]["stufe"] == "low" and w["browser.open"]["offline"] is False
