"""Konten und Zugangsdaten aus der App: nichts ohne Freigabe, Werte nie in Freigabe/Protokoll/Antwort,
Google-Link (PKCE) und Microsoft-Gerätecode, Tresor-Verwaltung nur für den Core, RunPod mit env-Datei."""

import asyncio
import base64
import hashlib
import json
import os
import socket
import stat
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import qulsi_tresor as t
from qulsi_core import konten

ROOT = Path(__file__).resolve().parents[2]
CONNECTORS = ROOT / "connectors"
GEHEIM = "S3hr-geheim!Passwort"


# ---------------------------------------------------------------- Hilfen


class FakeZugang:
    tresor = True

    def __init__(self):
        self.gesetzt, self.entfernt, self.eingeloest, self.abgemeldet = {}, [], [], []
        self.client_ids = {"GOOGLE_CLIENT_ID": "123.apps.googleusercontent.com", "MS_CLIENT_ID": "ms-id"}
        self.pruefungen = [{"error": "authorization_pending"}, {"verbunden": True, "scope": "Mail.Read"}]
        self.verbunden = set()

    async def uebersicht(self):
        return {"geheimnisse": {n: True for n in self.gesetzt}, "client_ids": self.client_ids,
                "konten": {c: {"verbunden": True, "scope": "", "seit": None} for c in self.verbunden}}

    async def setzen(self, werte):
        self.gesetzt.update(werte)
        return "verschlüsselt im Tresor gespeichert"

    async def entfernen(self, namen):
        self.entfernt += namen
        return "aus dem Tresor entfernt"

    async def einloesen(self, connector, k, felder):
        self.eingeloest.append((connector, felder))
        self.verbunden.add(connector)
        return {"verbunden": True, "scope": "calendar"}

    async def geraetecode(self, connector, k):
        return {"user_code": "ABCD-1234", "verification_uri": "https://microsoft.com/devicelogin",
                "interval": 0, "expires_in": 900}

    async def geraetecode_pruefen(self, connector, k):
        antwort = self.pruefungen.pop(0)
        if antwort.get("verbunden"):
            self.verbunden.add(connector)
        return antwort

    async def abmelden(self, connector):
        self.abgemeldet.append(connector)


class Welt:
    """Minimaler Core: Freigaben, Store, Hub und ntfy als Attrappen, die alles mitschreiben."""

    def __init__(self, zugang):
        self.freigaben_liste: list[dict] = []
        self.ereignisse: list[str] = []
        self.protokoll: list[tuple] = []
        self.ntfy: list[tuple] = []
        welt = self

        class Freigaben:
            gueltig = 300

            async def anlegen(self, tool, arguments, kanal, belastet, begruendung, vorschau=None):
                fid = f"00000000-0000-0000-0000-{len(welt.freigaben_liste):012d}"
                welt.freigaben_liste.append({"id": fid, "tool": tool, "arguments": json.dumps(arguments),
                                             "begruendung": begruendung, "vorschau": vorschau, "kanal": kanal,
                                             "belastet": belastet, "status": "offen"})
                return SimpleNamespace(id=fid, benachrichtigt=True)

        class Store:
            async def alle(self, sql, params=None):
                return [r for r in welt.freigaben_liste if r["status"] == "offen" and r["tool"].startswith("konto.")]

            async def ereignis(self, art, text, *a, **kw):
                welt.ereignisse.append(text)

        class Hub:
            async def freigabe_protokoll(self, row, entscheid, text=""):
                welt.protokoll.append((json.dumps(row["arguments"]) if isinstance(row["arguments"], dict)
                                       else row["arguments"], entscheid, text))

        class Notifier:
            async def senden(self, titel, text):
                welt.ntfy.append((titel, text))

        self.state = SimpleNamespace(
            settings=SimpleNamespace(connectors_dir=str(CONNECTORS), egress_url="http://egress:3128"),
            freigaben=Freigaben(), store=Store(), hub=Hub(), notifier=Notifier())
        self.state.konten = konten.Konten(str(CONNECTORS), zugang, 300)
        app = FastAPI()
        app.include_router(konten.router)
        app.state = self.state  # type: ignore[assignment]
        self.app = app
        self.client = TestClient(app, client=("192.168.178.20", 50000))  # aus dem Heimnetz

    def erlauben(self, nr: int = -1) -> str:
        row = self.freigaben_liste[nr]
        row["status"] = "freigegeben"
        return asyncio.run(konten.nach_freigabe(self.state, row))

    def alles_sichtbare(self) -> str:
        """Was in Datenbank, Protokoll, Timeline und auf dem Handy landet."""
        return json.dumps([self.freigaben_liste, self.ereignisse, self.protokoll, self.ntfy], ensure_ascii=False)


@pytest.fixture()
def welt():
    return Welt(FakeZugang())


# ---------------------------------------------------------------- Katalog aus den echten Manifesten


def test_katalog_aus_manifesten():
    kat = konten.katalog(CONNECTORS)
    assert kat["google"]["anmeldung"] == "link" and kat["google"]["namen"] == ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"]
    assert kat["google"]["konto"]["redirect_uri"].startswith("http://127.0.0.1")
    assert "gmail.compose" in kat["google"]["konto"]["scopes"]
    assert kat["microsoft"]["anmeldung"] == "geraetecode" and kat["microsoft"]["namen"] == ["MS_CLIENT_ID"]
    assert "offline_access" in kat["microsoft"]["konto"]["scopes"]
    assert kat["nas"]["namen"] == ["NAS_PASSWORD"] and kat["nas"]["anmeldung"] is None
    assert kat["mail"]["namen"] == ["IMAP_PASSWORD", "SMTP_PASSWORD"]
    assert kat["home_assistant"]["namen"] == ["HA_TOKEN"]
    assert "web" not in kat and "files" not in kat
    for info in kat.values():  # jeder Name hat einen Text für die App
        assert all(n in konten.BESCHREIBUNG for n in info["namen"])


def test_eingaben_pruefen(welt):
    k = welt.state.konten
    assert k.werte_pruefen("nas", {"NAS_PASSWORD": "  pw  "}) == {"NAS_PASSWORD": "pw"}
    assert k.werte_pruefen("mail", {"IMAP_PASSWORD": "a", "SMTP_PASSWORD": ""}) == {"IMAP_PASSWORD": "a"}
    for connector, werte, code in [("nas", {"IMAP_PASSWORD": "x"}, 422),     # fremder Name
                                   ("nas", {"NAS_PASSWORD": "a\nb"}, 422),  # Steuerzeichen
                                   ("nas", {"NAS_PASSWORD": "x" * 5000}, 422),
                                   ("nas", {"NAS_PASSWORD": "  "}, 422),     # nichts eingegeben
                                   ("nas", ["NAS_PASSWORD"], 422),
                                   ("web", {"X": "y"}, 404)]:
        with pytest.raises(HTTPException) as f:
            k.werte_pruefen(connector, werte)
        assert f.value.status_code == code


# ---------------------------------------------------------------- Zugangsdaten


def test_zugangsdaten_erst_nach_freigabe_und_nie_sichtbar(welt):
    r = welt.client.post("/api/konten/zugangsdaten", json={"connector": "nas", "werte": {"NAS_PASSWORD": GEHEIM}})
    assert r.status_code == 200 and r.json()["status"] == "freigabe_noetig" and r.json()["handy"] is True
    assert GEHEIM not in r.text
    zugang = welt.state.konten.zugang
    assert zugang.gesetzt == {}  # ohne Freigabe nichts im Tresor
    fr = welt.freigaben_liste[0]
    assert fr["tool"] == konten.SETZEN and "NAS_PASSWORD" in fr["vorschau"]
    uebersicht = welt.client.get("/api/konten").json()
    nas = next(c for c in uebersicht["connectoren"] if c["name"] == "nas")
    assert nas["offen"][0]["id"] == fr["id"] and nas["zugangsdaten"][0]["gesetzt"] is False

    ergebnis = welt.erlauben()
    assert "NAS_PASSWORD" in ergebnis and "Tresor" in ergebnis
    assert zugang.gesetzt == {"NAS_PASSWORD": GEHEIM}
    assert welt.state.konten.ausstehend == {}  # Wert ist aus dem Arbeitsspeicher des Core weg
    assert welt.erlauben().startswith("Fehler")  # die gleiche Freigabe wirkt nur einmal
    assert GEHEIM not in welt.alles_sichtbare()
    assert GEHEIM not in welt.client.get("/api/konten").text
    nas = next(c for c in welt.client.get("/api/konten").json()["connectoren"] if c["name"] == "nas")
    assert nas["zugangsdaten"][0]["gesetzt"] is True


def test_ablehnen_verwirft_den_wert(welt):
    welt.client.post("/api/konten/zugangsdaten", json={"connector": "mail", "werte": {"IMAP_PASSWORD": GEHEIM}})
    args = json.loads(welt.freigaben_liste[0]["arguments"])
    assert args["namen"] == ["IMAP_PASSWORD"] and GEHEIM not in json.dumps(args)
    konten.abgelehnt(welt.state, args)
    assert welt.state.konten.ausstehend == {}
    assert welt.erlauben().startswith("Fehler")
    assert welt.state.konten.zugang.gesetzt == {}


def test_freigabe_mit_anderen_namen_wirkt_nicht(welt):
    welt.client.post("/api/konten/zugangsdaten", json={"connector": "mail", "werte": {"IMAP_PASSWORD": GEHEIM}})
    row = welt.freigaben_liste[0]
    args = json.loads(row["arguments"])
    row["arguments"] = json.dumps({**args, "namen": ["SMTP_PASSWORD"]})
    assert welt.erlauben().startswith("Fehler")
    assert welt.state.konten.zugang.gesetzt == {}


def test_entfernen_und_spam_grenze(welt):
    r = welt.client.post("/api/konten/zugangsdaten/entfernen", json={"connector": "nas", "namen": ["HA_TOKEN"]})
    assert r.status_code == 422
    r = welt.client.post("/api/konten/zugangsdaten/entfernen", json={"connector": "nas", "namen": ["NAS_PASSWORD"]})
    assert r.json()["status"] == "freigabe_noetig" and welt.state.konten.zugang.entfernt == []
    welt.erlauben()
    assert welt.state.konten.zugang.entfernt == ["NAS_PASSWORD"]
    for _ in range(konten.MAX_OFFEN):
        welt.client.post("/api/konten/zugangsdaten", json={"connector": "nas", "werte": {"NAS_PASSWORD": "x"}})
    r = welt.client.post("/api/konten/zugangsdaten", json={"connector": "nas", "werte": {"NAS_PASSWORD": "x"}})
    assert r.status_code == 429
    assert len(welt.state.konten.ausstehend) == konten.MAX_OFFEN  # abgelehnte Eingabe nicht liegen gelassen


def test_ohne_tresor_und_ohne_runpod_nichts_einrichten():
    w = Welt(None)
    r = w.client.post("/api/konten/zugangsdaten", json={"connector": "nas", "werte": {"NAS_PASSWORD": "x"}})
    assert r.status_code == 503 and not w.freigaben_liste
    assert w.client.get("/api/konten").json()["verfuegbar"] is False


# ---------------------------------------------------------------- Google: Link mit PKCE


def test_google_verbinden_mit_link(welt):
    assert welt.client.get("/api/konten/google/anmeldung").json() == {"status": "keine"}
    assert welt.client.post("/api/konten/nas/verbinden").status_code == 409
    r = welt.client.post("/api/konten/google/verbinden")
    assert r.json()["status"] == "freigabe_noetig"
    assert welt.client.get("/api/konten/google/anmeldung").json()["status"] == "keine"  # erst nach Freigabe
    assert "Anmeldung gestartet" in welt.erlauben()
    a = welt.client.get("/api/konten/google/anmeldung").json()
    assert a["art"] == "link" and a["status"] == "offen"
    q = parse_qs(urlsplit(a["url"]).query)
    assert a["url"].startswith("https://accounts.google.com/") and q["client_id"] == ["123.apps.googleusercontent.com"]
    assert q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"]

    falsch = f"http://127.0.0.1:8765/?state=fremd&code=4/abc"
    assert welt.client.post("/api/konten/google/anmeldung", json={"antwort": falsch}).status_code == 422
    assert welt.client.post("/api/konten/google/anmeldung", json={"antwort": "4/abc"}).status_code == 422
    richtig = f"http://127.0.0.1:8765/?state={q['state'][0]}&code=4/abc&scope=x"
    r = welt.client.post("/api/konten/google/anmeldung", json={"antwort": richtig})
    assert r.json()["status"] == "verbunden"
    connector, felder = welt.state.konten.zugang.eingeloest[0]
    assert connector == "google" and felder["code"] == "4/abc" and felder["redirect_uri"] == "http://127.0.0.1:8765/"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(felder["code_verifier"].encode()).digest()).decode().rstrip("=")
    assert [challenge] == q["code_challenge"]
    assert welt.client.post("/api/konten/google/anmeldung", json={"antwort": richtig}).status_code == 409  # nur einmal
    assert welt.client.get("/api/konten/google/anmeldung").json()["status"] == "verbunden"
    assert felder["code_verifier"] not in welt.alles_sichtbare()
    assert ("Konto verbunden", "Google (Kalender, Gmail, Drive) ist jetzt mit QuLSI verbunden.") in welt.ntfy


def test_google_ohne_client_id(welt):
    welt.state.konten.zugang.client_ids = {}
    welt.client.post("/api/konten/google/verbinden")
    assert "GOOGLE_CLIENT_ID fehlt" in welt.erlauben()
    google = next(c for c in welt.client.get("/api/konten").json()["connectoren"] if c["name"] == "google")
    assert google["bereit"] is False and google["verbunden"] is False


# ---------------------------------------------------------------- Microsoft: Gerätecode


def test_microsoft_geraetecode(welt):
    welt.client.post("/api/konten/microsoft/verbinden")

    async def ablauf():
        row = welt.freigaben_liste[-1]
        text = await konten.nach_freigabe(welt.state, row)
        stand = welt.state.konten.anmeldung_stand("microsoft")
        await welt.state.konten.anmeldungen["microsoft"]["task"]
        return text, stand

    text, stand = asyncio.run(ablauf())
    assert "ABCD-1234" in text and stand["user_code"] == "ABCD-1234" and stand["status"] == "offen"
    assert welt.state.konten.anmeldung_stand("microsoft")["status"] == "verbunden"
    assert welt.state.konten.zugang.pruefungen == []
    assert "Microsoft (Outlook, Kalender, OneDrive): Konto verbunden" in welt.ereignisse


def test_trennen(welt):
    welt.client.post("/api/konten/microsoft/trennen")
    assert welt.state.konten.zugang.abgemeldet == []
    assert "getrennt" in welt.erlauben()
    assert welt.state.konten.zugang.abgemeldet == ["microsoft"]


def test_nur_konto_werkzeuge():
    assert konten.zustaendig("konto.zugangsdaten") and not konten.zustaendig("email.send")


# ---------------------------------------------------------------- Tresor: Verwaltung nur für den Core


@pytest.fixture()
def tresor(tmp_path, monkeypatch):
    monkeypatch.setattr(t, "CONNECTORS", CONNECTORS)
    monkeypatch.setattr(t, "DATA", tmp_path)
    monkeypatch.setenv("MS_TENANT", "common")
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    s = t.Speicher(tmp_path / "tresor.age", tmp_path / "schluessel.txt")
    s.schreiben({"geheimnisse": {"NAS_PASSWORD": "alt"}, "konten": {}})
    monkeypatch.setattr(t, "speicher", s)
    monkeypatch.setattr(t, "geraetecodes", {})
    rolle = {"core": True}

    async def fake_core(request):
        if not rolle["core"]:
            raise t.HTTPException(403, "Verwaltung nur für den Core")

    monkeypatch.setattr(t, "nur_core", fake_core)
    return TestClient(t.app), s, rolle


def test_tresor_verwaltung_schreibt_nur(tresor):
    client, s, rolle = tresor
    r = client.post("/verwaltung/geheimnisse", json={"werte": {"NAS_PASSWORD": GEHEIM, "GOOGLE_CLIENT_ID": "id-1"}})
    assert r.json() == {"gesetzt": ["GOOGLE_CLIENT_ID", "NAS_PASSWORD"]}
    assert s.lesen()["geheimnisse"]["NAS_PASSWORD"] == GEHEIM
    assert client.post("/verwaltung/geheimnisse", json={"werte": {"POSTGRES_PASSWORD": "x"}}).status_code == 422
    u = client.get("/verwaltung/uebersicht").json()
    assert GEHEIM not in json.dumps(u) and u["geheimnisse"]["NAS_PASSWORD"] is True and u["geheimnisse"]["HA_TOKEN"] is False
    assert u["client_ids"] == {"GOOGLE_CLIENT_ID": "id-1", "MS_CLIENT_ID": ""}  # nur Client-IDs, die im Link stehen
    client.post("/verwaltung/geheimnisse/entfernen", json={"namen": ["NAS_PASSWORD"]})
    assert "NAS_PASSWORD" not in s.lesen()["geheimnisse"]
    rolle["core"] = False
    for methode, pfad in [("GET", "/verwaltung/uebersicht"), ("POST", "/verwaltung/geheimnisse"),
                          ("POST", "/verwaltung/konto/google/einloesen"), ("POST", "/verwaltung/konto/microsoft/geraetecode"),
                          ("POST", "/verwaltung/konto/google/abmelden")]:
        assert client.request(methode, pfad, json={"werte": {"NAS_PASSWORD": "x"}}).status_code == 403


def test_tresor_einloesen_gibt_kein_token_heraus(tresor, monkeypatch):
    client, s, _ = tresor
    gesehen = []

    async def endpunkt(k, daten, felder):
        gesehen.append((k["token_url"], k.get("client_id"), felder))
        return {"access_token": "kurz", "refresh_token": "lang-lebig", "expires_in": 3600, "scope": "calendar"}

    monkeypatch.setattr(t, "_token_endpunkt", endpunkt)
    assert client.post("/verwaltung/konto/google/einloesen", json={"code": "c"}).status_code == 422
    r = client.post("/verwaltung/konto/google/einloesen",
                    json={"code": "c", "code_verifier": "v", "redirect_uri": "http://127.0.0.1:8765/", "grant_type": "refresh_token"})
    assert r.json() == {"verbunden": True, "scope": "calendar"}
    assert "lang-lebig" not in r.text and "kurz" not in r.text
    assert gesehen[0][2]["grant_type"] == "authorization_code"  # der Core kann keinen anderen Ablauf erzwingen
    assert gesehen[0][1] == "GOOGLE_CLIENT_ID"
    konto = s.lesen()["konten"]["google"]
    assert konto["refresh_token"] == "lang-lebig" and konto["seit"]
    assert client.get("/verwaltung/uebersicht").json()["konten"]["google"]["verbunden"] is True
    assert client.post("/verwaltung/konto/nas/einloesen",
                       json={"code": "c", "code_verifier": "v", "redirect_uri": "x"}).status_code == 403  # kein Konto
    client.post("/verwaltung/konto/google/abmelden")
    assert "google" not in s.lesen()["konten"]


def test_tresor_geraetecode_behaelt_device_code(tresor, monkeypatch):
    client, s, _ = tresor
    s.schreiben({"geheimnisse": {"MS_CLIENT_ID": "ms-id"}, "konten": {}})
    posts = []

    class Antwort:
        status_code = 200

        def __init__(self, d):
            self.d = d

        def json(self):
            return self.d

    class Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data):
            posts.append((url, data))
            return Antwort({"device_code": "GEHEIMER-GERAETECODE", "user_code": "WXYZ", "interval": 5,
                            "verification_uri": "https://microsoft.com/devicelogin", "expires_in": 900})

    monkeypatch.setattr(t.httpx, "AsyncClient", Client)
    r = client.post("/verwaltung/konto/microsoft/geraetecode")
    assert r.json()["user_code"] == "WXYZ" and "GEHEIMER" not in r.text
    assert posts[0][0] == "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode"
    assert posts[0][1] == {"client_id": "ms-id", "scope": konten.katalog(CONNECTORS)["microsoft"]["konto"]["scopes"]}
    antworten = [{"error": "authorization_pending"}, {"access_token": "a", "refresh_token": "r", "expires_in": 10}]

    async def endpunkt(k, daten, felder):
        assert felder["device_code"] == "GEHEIMER-GERAETECODE"
        return antworten.pop(0)

    monkeypatch.setattr(t, "_token_endpunkt", endpunkt)
    assert client.post("/verwaltung/konto/microsoft/geraetecode/pruefen").json()["error"] == "authorization_pending"
    assert client.post("/verwaltung/konto/microsoft/geraetecode/pruefen").json()["verbunden"] is True
    assert client.post("/verwaltung/konto/microsoft/geraetecode/pruefen").json()["error"] == "expired_token"
    assert client.post("/verwaltung/konto/google/geraetecode").status_code == 409


def test_nur_core_erkennt_den_core(monkeypatch):
    def anfrage(ip):
        return SimpleNamespace(client=SimpleNamespace(host=ip))

    namen = {"172.20.0.5": "qulsi-core-1.qulsi_zone-agent", "172.20.0.6": "qulsi-connector-mail-1",
             "172.20.0.7": "qulsi-core-backup-1"}

    def rdns(ip):
        if ip not in namen:
            raise socket.herror("unbekannt")
        return (namen[ip], [], [ip])

    monkeypatch.setattr(t.socket, "gethostbyaddr", rdns)
    asyncio.run(t.nur_core(anfrage("172.20.0.5")))
    for ip in ("172.20.0.6", "172.20.0.7", "172.20.0.9", "127.0.0.1", "kaputt"):
        with pytest.raises(t.HTTPException) as f:
            asyncio.run(t.nur_core(anfrage(ip)))
        assert f.value.status_code == 403


# ---------------------------------------------------------------- RunPod: env-Datei und Token-Datei


def test_runpod_env_datei(tmp_path, monkeypatch):
    env = tmp_path / "env"
    env.write_text("POSTGRES_PASSWORD='pg'\nNAS_PASSWORD='alt'\n# Kommentar\n")
    z = konten.PodZugang(str(env), str(tmp_path / "konten"), "", "http://127.0.0.1:3128",
                         lambda: konten.katalog(CONNECTORS))
    text = asyncio.run(z.setzen({"NAS_PASSWORD": GEHEIM, "HA_TOKEN": "t"}))
    assert "nur Test-Konten" in text and "nas" in text and "home_assistant" in text  # Hinweis zum Neustart
    inhalt = env.read_text()
    assert f"NAS_PASSWORD='{GEHEIM}'" in inhalt and "alt" not in inhalt and "POSTGRES_PASSWORD='pg'" in inhalt
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    u = asyncio.run(z.uebersicht())
    assert u["geheimnisse"]["NAS_PASSWORD"] is True and u["geheimnisse"]["IMAP_PASSWORD"] is False
    asyncio.run(z.entfernen(["HA_TOKEN"]))
    assert "HA_TOKEN" not in env.read_text() and "POSTGRES_PASSWORD" in env.read_text()
    k = konten.Konten(str(CONNECTORS), z, 300)
    with pytest.raises(HTTPException):
        k.werte_pruefen("nas", {"NAS_PASSWORD": "mit'hochkomma"})


def test_runpod_token_datei_passt_zum_connector(tmp_path, monkeypatch):
    from qulsi_connector.konto import Konto

    env = tmp_path / "env"
    env.write_text("GOOGLE_CLIENT_ID=id-1\nGOOGLE_CLIENT_SECRET='sec'\n")
    z = konten.PodZugang(str(env), str(tmp_path / "konten"), "", "http://127.0.0.1:3128",
                         lambda: konten.katalog(CONNECTORS))
    gesehen = []

    async def anbieter(connector, url, felder):
        gesehen.append((connector, url, felder))
        return {"access_token": "a", "refresh_token": "r", "expires_in": 3600, "scope": "calendar"}

    monkeypatch.setattr(z, "_anbieter", anbieter)
    k = konten.katalog(CONNECTORS)["google"]["konto"]
    r = asyncio.run(z.einloesen("google", k, {"grant_type": "authorization_code", "code": "c"}))
    assert r == {"verbunden": True, "scope": "calendar"}
    assert gesehen[0][0] == "google" and gesehen[0][2]["client_id"] == "id-1" and gesehen[0][2]["client_secret"] == "sec"
    assert z._client("google")  # Proxy mit Connector-Namen baut sich
    konto = Konto("google", k["token_url"], "id-1", verzeichnis=tmp_path / "konten" / "google")
    assert konto.verbunden() and konto.laden()["refresh_token"] == "r"
    assert stat.S_IMODE((tmp_path / "konten" / "google" / "google.json").stat().st_mode) == 0o600
    assert asyncio.run(z.uebersicht())["konten"]["google"]["verbunden"] is True
    asyncio.run(z.abmelden("google"))
    assert not konto.verbunden()


def test_aus_umgebung(monkeypatch, tmp_path):
    monkeypatch.setenv("TRESOR_URL", "http://tresor:8400")
    assert isinstance(konten.Konten.aus_umgebung(str(CONNECTORS), "http://egress:3128", 300).zugang, konten.TresorZugang)
    monkeypatch.delenv("TRESOR_URL")
    monkeypatch.setenv("QULSI_LAUFZEIT", "runpod")
    monkeypatch.setenv("QULSI_ENV_FILE", str(tmp_path / "env"))
    assert isinstance(konten.Konten.aus_umgebung(str(CONNECTORS), "http://127.0.0.1:3128", 300).zugang, konten.PodZugang)
    monkeypatch.delenv("QULSI_LAUFZEIT")
    assert konten.Konten.aus_umgebung(str(CONNECTORS), "", 300).zugang is None


# ---------------------------------------------------------------- Connectoren lesen neue Passwörter ohne Neustart


def test_geheimnis_aktuell(monkeypatch):
    import qulsi_connector as nc

    werte = iter(["eins", "zwei"])
    aufrufe = []

    def get(url, **kw):
        aufrufe.append(url)
        if len(aufrufe) == 3:
            raise nc.httpx.ConnectError("weg")
        return SimpleNamespace(status_code=200, json=lambda: {"wert": next(werte)}, text="")

    monkeypatch.setenv("TRESOR_URL", "http://tresor:8400")
    monkeypatch.setattr(nc.httpx, "get", get)
    monkeypatch.setattr(nc, "_frisch", {})
    assert nc.geheimnis_aktuell("HA_TOKEN") == "eins"
    assert nc.geheimnis_aktuell("HA_TOKEN") == "eins" and len(aufrufe) == 1  # zwischengespeichert
    assert nc.geheimnis_aktuell("HA_TOKEN", sekunden=0) == "zwei"
    assert nc.geheimnis_aktuell("HA_TOKEN", sekunden=0) == "zwei"  # Tresor kurz weg: letzter Wert
    monkeypatch.delenv("TRESOR_URL")
    monkeypatch.setenv("HA_TOKEN", "aus-env")
    assert nc.geheimnis_aktuell("HA_TOKEN", sekunden=0) == "aus-env"


# ---------------------------------------------------------------- Zusätze: Heimnetz, Schalter, Meldung


def test_aenderung_nur_aus_heimnetz_oder_tailscale(welt, monkeypatch):
    monkeypatch.delenv("QULSI_LAUFZEIT", raising=False)
    draussen = TestClient(welt.app, client=("203.0.113.7", 40000))
    r = draussen.post("/api/konten/zugangsdaten", json={"connector": "nas", "werte": {"NAS_PASSWORD": GEHEIM}})
    assert r.status_code == 403 and "Heimnetz" in r.json()["detail"] and not welt.freigaben_liste
    assert draussen.get("/api/konten").json()["verfuegbar"] is False
    tailscale = TestClient(welt.app, client=("100.101.102.103", 40000))
    assert tailscale.post("/api/konten/nas/verbinden").status_code == 409  # Netz ok, NAS hat nur Zugangsdaten
    monkeypatch.setenv("QULSI_LAUFZEIT", "runpod")  # dort kommt alles über RunPods Proxy
    assert draussen.post("/api/konten/zugangsdaten", json={"connector": "nas", "werte": {"NAS_PASSWORD": GEHEIM}}).status_code == 200


def test_schalter_aus(welt, monkeypatch):
    monkeypatch.setenv("QULSI_TRESOR_APP", "aus")
    r = welt.client.post("/api/konten/zugangsdaten", json={"connector": "nas", "werte": {"NAS_PASSWORD": GEHEIM}})
    assert r.status_code == 403 and "QULSI_TRESOR_APP=aus" in r.json()["detail"]
    u = welt.client.get("/api/konten").json()
    assert u["verfuegbar"] is False and "abgeschaltet" in u["fehler"]


def test_jede_aenderung_meldet_ntfy(welt):
    welt.client.post("/api/konten/zugangsdaten", json={"connector": "nas", "werte": {"NAS_PASSWORD": GEHEIM}})
    welt.erlauben()
    assert welt.ntfy[-1][0] == "Zugangsdaten geändert" and "NAS_PASSWORD" in welt.ntfy[-1][1]
    assert GEHEIM not in welt.alles_sichtbare()


def test_tresor_lokal_nur_mit_schalter(monkeypatch):
    """RunPod: Verwaltung über 127.0.0.1 nur mit TRESOR_LOKAL=1 und Kopf X-QuLSI-Connector: core."""
    req = lambda kopf: SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers=kopf)  # noqa: E731
    monkeypatch.setattr(t, "LOKAL", False)
    with pytest.raises(HTTPException):
        asyncio.run(t.nur_core(req({"x-qulsi-connector": "core"})))
    monkeypatch.setattr(t, "LOKAL", True)
    asyncio.run(t.nur_core(req({"x-qulsi-connector": "core"})))
    with pytest.raises(HTTPException):
        asyncio.run(t.nur_core(req({"x-qulsi-connector": "nas"})))


def test_tresor_uebernimmt_konto_datei(tmp_path, monkeypatch):
    import io
    import sys
    s = t.Speicher(tmp_path / "tresor.age", tmp_path / "schluessel.txt")
    monkeypatch.setattr(t, "Speicher", lambda: s)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"access_token": "a", "refresh_token": "r", "scope": "x"})))
    t.befehl(["konto-importieren", "google"])
    assert s.lesen()["konten"]["google"]["refresh_token"] == "r"
    monkeypatch.setattr(sys, "stdin", io.StringIO("NAS_PASSWORD='geheim'\nPOSTGRES_PASSWORD=x\n"))
    monkeypatch.setattr(t, "CONNECTORS", CONNECTORS)
    t.befehl(["importieren"])
    assert s.lesen()["geheimnisse"] == {"NAS_PASSWORD": "geheim"}
