import asyncio
from pathlib import Path

from qulsi_egress import Connector, Egress, connectoren_laden, regeln_aus

ROOT = Path(__file__).resolve().parents[2]


def test_regeln_aus_env_und_urls():
    env = {"ICS": "Privat=https://calendar.google.com/a.ics;Arbeit=webcal://p01.icloud.com/b",
           "HA": "http://192.168.1.10:8123", "IMAP": ""}
    r = regeln_aus("${ICS}", env)
    assert [(x.host, x.ports) for x in r] == [("calendar.google.com", {443}), ("p01.icloud.com", {443})]
    assert [(x.host, x.ports) for x in regeln_aus("${HA}", env)] == [("192.168.1.10", {8123})]
    assert regeln_aus("${IMAP}:993", env) == []  # nicht eingerichtet -> nichts erlaubt


def test_wildcard_und_ports():
    c = Connector("x", "connector-x", regeln_aus("*.example.com") + regeln_aus("imap.example.org:993"))
    assert c.erlaubt("api.example.com", 443) and c.erlaubt("example.com", 80)
    assert not c.erlaubt("example.com.evil.org", 443)
    assert not c.erlaubt("api.example.com", 22)
    assert c.erlaubt("IMAP.example.org", 993) and not c.erlaubt("imap.example.org", 443)


def test_manifeste_ohne_internet():
    cs = connectoren_laden(str(ROOT / "connectors"))
    assert cs["files"].regeln == [] and cs["vision"].regeln == [] and cs["web"].regeln == []


def test_identitaet_per_dns_und_loopback(monkeypatch):
    cs = {"calendar": Connector("calendar", "connector-calendar", [])}
    e = Egress(cs, [])
    import socket

    monkeypatch.setattr(socket, "gethostbyaddr", lambda ip: ("qulsi-connector-calendar-1.qulsi_zone-connectors", [], [ip]))
    assert asyncio.run(e.wer("172.20.0.5", {})) == "calendar"
    monkeypatch.setattr(socket, "gethostbyaddr", lambda ip: ("qulsi-core-1.qulsi_zone-agent", [], [ip]))
    assert asyncio.run(e.wer("172.20.0.6", {"proxy-authorization": "Basic Y2FsZW5kYXI6eA=="})) is None  # Header zählt nur lokal
    assert asyncio.run(e.wer("127.0.0.1", {"proxy-authorization": "Basic Y2FsZW5kYXI6eA=="})) == "calendar"


def test_manifest_v2_domains_und_ports(tmp_path):
    (tmp_path / "g").mkdir()
    (tmp_path / "g" / "manifest.yaml").write_text(
        "name: g\nnetwork:\n  allowed_domains: [www.googleapis.com, '*.example.org']\n  allowed_ports: [443]\n"
        "  allowed: ['nas.local:445']\nlimits: {anfragen_pro_minute: 2}\n")
    c = connectoren_laden(str(tmp_path))["g"]
    assert c.erlaubt("www.googleapis.com", 443) and not c.erlaubt("www.googleapis.com", 80)
    assert c.erlaubt("a.example.org", 443) and c.erlaubt("nas.local", 445)
    assert c.limits["anfragen_pro_minute"] == 2 and c.limits["mb_pro_minute"] == 50
    assert c.domains == ["nas.local", "www.googleapis.com"]


def test_limit_und_quarantaene(tmp_path):
    cs = {"g": Connector("g", "connector-g", [], {"anfragen_pro_minute": 2, "mb_pro_minute": 1})}
    e = Egress(cs, [], str(tmp_path))
    assert e.anfrage_zaehlen("g", 0) and e.anfrage_zaehlen("g", 1) and not e.anfrage_zaehlen("g", 2)
    assert e.anfrage_zaehlen("g", 61)  # neue Minute
    for i in range(9):
        e.ablehnung("g", jetzt=i)
    assert "g" not in e.quarantaene
    e.ablehnung("g", jetzt=9)
    assert "10 abgelehnte" in e.quarantaene["g"]["grund"]
    # bleibt nach einem Neustart bestehen, bis der Core freigibt
    e2 = Egress(cs, [], str(tmp_path))
    assert "g" in e2.quarantaene and e2.freigeben("g") and "g" not in Egress(cs, [], str(tmp_path)).quarantaene


def test_ablehnungen_verfallen_nach_10_minuten(tmp_path):
    e = Egress({"g": Connector("g", "connector-g", [])}, [], str(tmp_path))
    for i in range(9):
        e.ablehnung("g", jetzt=i)
    e.ablehnung("g", jetzt=700)
    assert e.quarantaene == {}


def test_protokoll_als_json_zeilen(tmp_path):
    import json

    e = Egress({}, [], str(tmp_path))
    e.protokoll(connector="g", ziel="x.org", port=443, ergebnis="verweigert")
    zeile = json.loads((tmp_path / "egress.jsonl").read_text().splitlines()[0])
    assert zeile["connector"] == "g" and zeile["ergebnis"] == "verweigert" and "zeit" in zeile


def test_signatur_sperrt_veraenderte_connectoren(tmp_path, monkeypatch):
    import base64
    import json

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from qulsi_integritaet import nachricht, pruefen, summen

    d = tmp_path / "connectors"
    for n in ("a", "b"):
        (d / n).mkdir(parents=True)
        (d / n / "manifest.yaml").write_text(f"name: {n}\n")
        (d / n / "server.py").write_text("print(1)\n")
    (d / "_lib").mkdir()
    (d / "_lib" / "x.py").write_text("")
    k = Ed25519PrivateKey.generate()
    pub = tmp_path / "signatur.pub"
    pub.write_text(base64.b64encode(k.public_key().public_bytes(serialization.Encoding.Raw,
                                                                serialization.PublicFormat.Raw)).decode())
    werte = summen(d)
    (d / "SIGNATUR.json").write_text(json.dumps({"connectoren": werte, "signatur": base64.b64encode(k.sign(nachricht(werte))).decode()}))
    assert pruefen(d, pub) == ({}, None)
    (d / "b" / "server.py").write_text("print('boese')\n")
    assert pruefen(d, pub) == ({"b": "Dateien wurden nach dem Signieren verändert"}, None)
    (d / "_lib" / "x.py").write_text("import os\n")
    assert pruefen(d, pub)[1] is not None
    assert pruefen(d, None) == ({}, None)  # ohne Schlüssel keine Prüfung


def test_integritaet_in_core_und_egress_gleich():
    assert (ROOT / "egress/qulsi_integritaet.py").read_bytes() == (ROOT / "core/qulsi_core/integritaet.py").read_bytes()


def test_tresor_darf_nur_zu_token_endpunkten():
    cs = connectoren_laden(str(ROOT / "connectors"))
    t = cs["tresor"]
    assert t.erlaubt("oauth2.googleapis.com", 443) and t.erlaubt("login.microsoftonline.com", 443)
    assert not t.erlaubt("www.googleapis.com", 443) and not t.erlaubt("example.org", 443)


def test_qulsi_selbst_ist_tabu():
    # Fall RunPod: browser.open rief https://<pod>-8100.proxy.runpod.net/api/... auf
    import qulsi_egress as e
    eigene = e.eigene_hosts({"PUBLIC_URL": "https://abcdefghijklmn-8100.proxy.runpod.net",
                             "NTFY_PUBLIC_URL": "https://abcdefghijklmn-8090.proxy.runpod.net",
                             "RUNPOD_POD_ID": "abcdefghijklmn", "QULSI_EIGENE_HOSTS": "meine-box.dyndns.org"})
    for host in ("abcdefghijklmn-8100.proxy.runpod.net", "ABCDEFGHIJKLMN-3000.proxy.runpod.net.", "meine-box.dyndns.org"):
        assert e.ist_eigener_host(host, eigene), host
    assert not e.ist_eigener_host("anderer-8100.proxy.runpod.net", eigene)
    assert not e.ist_eigener_host("www.welt.de", eigene)
    assert e.eigene_hosts({"PUBLIC_URL": "http://qulsi.local"}) == []
