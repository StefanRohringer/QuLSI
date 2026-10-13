"""Tresor: Geheimnisse nur laut Manifest, Refresh-Token verlässt den Tresor nie."""

import json

import pytest
from fastapi.testclient import TestClient

import qulsi_tresor as t


@pytest.fixture()
def tresor(tmp_path, monkeypatch):
    c = tmp_path / "connectors"
    for name, extra in {"mail": "secrets: [IMAP_PASSWORD]\n",
                        "google": "konto:\n  token_url: https://oauth2.example/token\n  client_secret: G_SECRET\n",
                        "web": ""}.items():
        (c / name).mkdir(parents=True)
        (c / name / "manifest.yaml").write_text(f"name: {name}\n{extra}")
    monkeypatch.setattr(t, "CONNECTORS", c)
    monkeypatch.setattr(t, "DATA", tmp_path)
    s = t.Speicher(tmp_path / "tresor.age", tmp_path / "schluessel.txt")
    s.schreiben({"geheimnisse": {"IMAP_PASSWORD": "geheim", "G_SECRET": "client-geheim"}, "konten": {}})
    monkeypatch.setattr(t, "speicher", s)
    wer = {"name": "mail"}

    async def fake_wer(request, ms):
        return wer["name"]

    monkeypatch.setattr(t, "wer", fake_wer)
    return TestClient(t.app), wer, s, tmp_path


def test_geheimnis_nur_laut_manifest(tresor):
    client, wer, _, tmp = tresor
    assert client.get("/geheimnis/IMAP_PASSWORD").json() == {"wert": "geheim"}
    wer["name"] = "web"
    assert client.get("/geheimnis/IMAP_PASSWORD").status_code == 403
    wer["name"] = "google"
    assert client.get("/geheimnis/G_SECRET").status_code == 403  # Client-Secret nie an den Connector
    zugriffe = [json.loads(z) for z in (tmp / "zugriffe.jsonl").read_text().splitlines()]
    assert [z["ergebnis"] for z in zugriffe] == ["ok", "verweigert", "verweigert"]


def test_datei_ist_verschluesselt(tresor):
    _, _, s, tmp = tresor
    roh = (tmp / "tresor.age").read_bytes()
    assert roh.startswith(b"age-encryption.org") and b"geheim" not in roh


def test_oauth_refresh_token_bleibt_im_tresor(tresor, monkeypatch):
    client, wer, s, _ = tresor
    wer["name"] = "google"
    gesehen = []

    async def endpunkt(k, daten, felder):
        anfrage = dict(felder, client_secret=daten["geheimnisse"][k["client_secret"]])
        gesehen.append(anfrage)
        return {"access_token": f"kurz-{len(gesehen)}", "refresh_token": "lang", "expires_in": 3600}

    monkeypatch.setattr(t, "_token_endpunkt", endpunkt)
    r = client.post("/konto/token", json={"grant_type": "authorization_code", "code": "abc"}).json()
    assert r["access_token"] == "kurz-1" and r["refresh_token"] == "im-tresor"
    assert s.lesen()["konten"]["google"]["refresh_token"] == "lang"
    assert client.get("/token").json()["access_token"] == "kurz-1"
    assert client.get("/token", params={"erneuern": "1"}).json()["access_token"] == "kurz-2"
    assert gesehen[1]["grant_type"] == "refresh_token" and gesehen[1]["client_secret"] == "client-geheim"
    wer["name"] = "mail"
    assert client.get("/token").status_code == 403  # mail hat kein Konto
