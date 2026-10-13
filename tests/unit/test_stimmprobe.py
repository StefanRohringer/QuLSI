"""Stimmprobe aus der App: Prüfung der Aufnahme, welches Modell klonen kann, Übernahme erst nach Freigabe."""

import asyncio
import io
import uuid
import wave

import pytest
from fastapi import HTTPException

from qulsi_core import stimmprobe


def wav(sekunden: float, rate: int = 24000, breite: int = 2, kanaele: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(kanaele)
        w.setsampwidth(breite)
        w.setframerate(rate)
        w.writeframes(b"\0" * int(sekunden * rate) * breite * kanaele)
    return buf.getvalue()


def test_wav_pruefen():
    assert stimmprobe.wav_pruefen(wav(8)) == pytest.approx(8)
    assert stimmprobe.wav_pruefen(wav(5, 16000, kanaele=2)) == pytest.approx(5)
    assert stimmprobe.wav_pruefen(wav(15.4)) == pytest.approx(15.4)  # App stoppt bei 15 s, Datei etwas länger
    for daten, code in [(wav(3), 422), (wav(16.5), 422), (wav(8, breite=1), 415), (wav(8, 8000), 415),
                        (b"OggS kein wav", 415), (b"\0" * (stimmprobe.MAX_BYTES + 1), 413)]:
        with pytest.raises(HTTPException) as f:
            stimmprobe.wav_pruefen(daten)
        assert f.value.status_code == code


def test_faehigkeit(monkeypatch):
    antworten = {}

    class Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            if antworten["h"] is None:
                raise OSError("weg")
            return type("R", (), {"json": lambda self: antworten["h"]})()

    monkeypatch.setattr(stimmprobe.httpx, "AsyncClient", Client)
    for h, klonen in [({"tts": "openai+piper", "tts_modell": "qwen3-tts"}, True),
                      ({"tts": "openai", "tts_modell": "chatterbox"}, True),
                      ({"tts": "piper", "tts_modell": None}, False),
                      ({"tts": "openai", "tts_modell": "gpt-4o-mini-tts"}, False), (None, False)]:
        antworten["h"] = h
        f = asyncio.run(stimmprobe.faehigkeit("http://voice"))
        assert f["klonen"] is klonen and (klonen or f["hinweis"])


def test_uebernehmen_und_verwerfen(tmp_path, monkeypatch):
    monkeypatch.setenv("STIMME_DIR", str(tmp_path))
    (tmp_path / "stimme.wav").write_bytes(b"alt")
    probe = str(uuid.uuid4())
    (tmp_path / "ausstehend").mkdir()
    (tmp_path / "ausstehend" / f"{probe}.wav").write_bytes(b"neu")
    (tmp_path / "ausstehend" / f"{probe}.txt").write_text("Hallo", encoding="utf-8")
    text = asyncio.run(stimmprobe.uebernehmen({"probe": probe, "sekunden": 8.0}, ""))
    assert "übernommen" in text
    assert (tmp_path / "stimme.wav").read_bytes() == b"neu" and (tmp_path / "stimme.txt").read_text() == "Hallo"
    assert (tmp_path / "stimme.vorher.wav").read_bytes() == b"alt"
    assert asyncio.run(stimmprobe.uebernehmen({"probe": probe}, "")).startswith("Fehler")  # nur einmal
    # Ablehnen löscht die wartende Probe; Pfade aus der Datenbank müssen UUIDs sein
    (tmp_path / "ausstehend" / f"{probe}.wav").write_bytes(b"x")
    stimmprobe.verwerfen({"probe": probe})
    assert not list((tmp_path / "ausstehend").iterdir())
    with pytest.raises(ValueError):
        stimmprobe.verwerfen({"probe": "../stimme"})


def test_uebernehmen_ohne_erreichbaren_tts_dienst(tmp_path, monkeypatch):
    """Ist der TTS-Dienst gerade nicht da, gilt die Stimme trotzdem (der Dienst prüft stimme.wav vor jedem Satz)."""
    monkeypatch.setenv("STIMME_DIR", str(tmp_path))
    probe = str(uuid.uuid4())
    (tmp_path / "ausstehend").mkdir()
    (tmp_path / "ausstehend" / f"{probe}.wav").write_bytes(b"neu")
    text = asyncio.run(stimmprobe.uebernehmen({"probe": probe, "sekunden": 8.0}, "http://127.0.0.1:9"))
    assert "übernommen" in text and (tmp_path / "stimme.wav").read_bytes() == b"neu"


def test_text_der_probe_wird_erkannt(monkeypatch):
    """Ohne Text nur Sprecher-Merkmale (klingt weniger ähnlich), deshalb erkennt Core den Text selbst."""
    import httpx

    antworten = iter([{"text": "  Hallo,  ich bin   die neue Stimme. "}, {"text": "Äh"}])

    def handler(request):
        assert request.url.path == "/stt" and b"probe.wav" in request.content
        return httpx.Response(200, json=next(antworten))

    echt = httpx.AsyncClient
    monkeypatch.setattr(stimmprobe.httpx, "AsyncClient",
                        lambda **kw: echt(transport=httpx.MockTransport(handler), **kw))
    assert asyncio.run(stimmprobe.text_erkennen("http://voice", b"RIFF")) == "Hallo, ich bin die neue Stimme."
    assert asyncio.run(stimmprobe.text_erkennen("http://voice", b"RIFF")) == ""  # zu wenig, lieber ohne Text
    assert asyncio.run(stimmprobe.text_erkennen("http://127.0.0.1:9", b"RIFF")) == ""
