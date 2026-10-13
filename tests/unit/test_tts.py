import io
import wave

from fastapi.testclient import TestClient


def test_tts_dienst_erzeugt_wav_und_laedt_erst_bei_bedarf(monkeypatch):
    monkeypatch.setenv("TTS_LEERLAUF_SEKUNDEN", "600")
    import qulsi_tts

    monkeypatch.setattr(qulsi_tts, "MODELL", "fake")
    with TestClient(qulsi_tts.app) as c:
        gesund = c.get("/health").json()
        assert gesund.pop("stimmen")["sprecher"] == ["ryan", "vivian"]
        assert gesund == {"ok": True, "modell": "fake", "geladen": False, "beschaeftigt_sekunden": None}
        r = c.post("/v1/audio/speech", json={"model": "fake", "input": "Hallo Anna.", "response_format": "wav"})
        assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
        with wave.open(io.BytesIO(r.content)) as wf:
            assert wf.getframerate() == 24000 and wf.getnframes() > 0
        assert c.get("/health").json()["geladen"] is True
        assert c.post("/v1/audio/speech", json={"input": " "}).status_code == 400
        # Neue eigene Stimme: das Modell bleibt geladen (die Referenz rechnet der nächste Satz neu)
        assert c.post("/stimme/neu").json()["ok"] is True
        assert c.get("/health").json()["geladen"] is True


def test_voice_nimmt_piper_wenn_tts_dienst_ausfaellt():
    import asyncio

    from qulsi_voice.main import FakeTTS, MitErsatz

    class Kaputt:
        name = "openai"

        async def sprechen(self, text, stimme):
            raise ConnectionError("tts nicht erreichbar")

    audio, typ = asyncio.run(MitErsatz(Kaputt(), FakeTTS()).sprechen("Hallo", None))
    assert typ == "audio/wav" and audio[:4] == b"RIFF"


def test_vorwaermen_laedt_das_modell_schon_vor_der_ersten_antwort(monkeypatch):
    import time

    monkeypatch.setenv("TTS_LEERLAUF_SEKUNDEN", "600")
    import qulsi_tts

    monkeypatch.setattr(qulsi_tts, "MODELL", "fake")
    with TestClient(qulsi_tts.app) as c:
        assert c.post("/vorwaermen").json() == {"ok": True, "geladen": False}
        ende = time.time() + 5
        while not c.get("/health").json()["geladen"] and time.time() < ende:
            time.sleep(0.05)
        assert c.get("/health").json()["geladen"] is True
        assert c.post("/vorwaermen").json()["geladen"] is True


def test_haengende_erzeugung_blockiert_nicht_alles(monkeypatch, tmp_path):
    """Pod 0906: eine Erzeugung kam nie zurück, danach hing jede Anfrage am Lock („Load failed“ in der App)."""
    import threading

    monkeypatch.setenv("TTS_LEERLAUF_SEKUNDEN", "600")
    import qulsi_tts

    los = threading.Event()

    class Langsam(qulsi_tts.Fake):
        def erzeugen(self, text, stimme):
            if text == "haengt":
                los.wait(10)
            return super().erzeugen(text, stimme)

    monkeypatch.setattr(qulsi_tts, "MODELL", "fake")
    monkeypatch.setitem(qulsi_tts.MODELLKLASSEN, "fake", Langsam)
    monkeypatch.setattr(qulsi_tts, "HOECHSTENS", 0.3)
    monkeypatch.setattr(qulsi_tts, "WARTEN", 0.2)
    monkeypatch.setattr(qulsi_tts, "STIMME", tmp_path)
    with TestClient(qulsi_tts.app) as c:
        assert c.post("/v1/audio/speech", json={"input": "haengt"}).status_code == 504   # Voice nimmt dann Piper
        assert c.get("/health").json()["beschaeftigt_sekunden"] is not None
        assert c.post("/v1/audio/speech", json={"input": "Hallo"}).status_code == 503    # sofort, nicht ewig
        assert c.post("/stimme/neu").json()["ok"] is True                                # wartet nicht auf das Lock
        los.set()
        for _ in range(50):
            if c.get("/health").json()["beschaeftigt_sekunden"] is None:
                break
            threading.Event().wait(0.05)
        assert c.post("/v1/audio/speech", json={"input": "Hallo"}).status_code == 200


def test_neue_stimme_wird_vor_dem_naechsten_satz_geladen(monkeypatch, tmp_path):
    import os
    import time

    monkeypatch.setenv("TTS_LEERLAUF_SEKUNDEN", "600")
    import qulsi_tts

    geladen = []

    class Zaehlt(qulsi_tts.Fake):
        def __init__(self):
            geladen.append(qulsi_tts.stimme_stand())

    monkeypatch.setattr(qulsi_tts, "MODELL", "fake")
    monkeypatch.setitem(qulsi_tts.MODELLKLASSEN, "fake", Zaehlt)
    monkeypatch.setattr(qulsi_tts, "STIMME", tmp_path)
    (tmp_path / "stimme.wav").write_bytes(b"alt")
    with TestClient(qulsi_tts.app) as c:
        c.post("/v1/audio/speech", json={"input": "Eins"})
        c.post("/v1/audio/speech", json={"input": "Zwei"})
        assert len(geladen) == 1
        (tmp_path / "stimme.wav").write_bytes(b"neu")   # Freigabe kam an, /stimme/neu aber nicht
        os.utime(tmp_path / "stimme.wav", (time.time() + 5, time.time() + 5))
        c.post("/v1/audio/speech", json={"input": "Drei"})
        assert len(geladen) == 2
        # Nur die Änderungszeit springt (Netzspeicher), Inhalt gleich: nicht neu laden
        os.utime(tmp_path / "stimme.wav", (time.time() + 50, time.time() + 50))
        c.post("/v1/audio/speech", json={"input": "Vier"})
        assert len(geladen) == 2


def test_neue_stimme_ohne_neu_laden(monkeypatch, tmp_path):
    """Qwen3-TTS: eine neue Probe berechnet nur die Stimmreferenz neu, das Modell bleibt im Grafikspeicher."""
    import os
    import time

    monkeypatch.setenv("TTS_LEERLAUF_SEKUNDEN", "0")
    import qulsi_tts

    geladen, neu = [], []

    class Klont(qulsi_tts.Fake):
        def __init__(self):
            geladen.append(1)

        def neue_stimme(self):
            neu.append(qulsi_tts.referenz()[1])

    monkeypatch.setattr(qulsi_tts, "MODELL", "fake")
    monkeypatch.setitem(qulsi_tts.MODELLKLASSEN, "fake", Klont)
    monkeypatch.setattr(qulsi_tts, "STIMME", tmp_path)
    (tmp_path / "stimme.wav").write_bytes(b"alt")
    with TestClient(qulsi_tts.app) as c:
        c.post("/v1/audio/speech", json={"input": "Eins"})
        (tmp_path / "stimme.wav").write_bytes(b"neu")
        (tmp_path / "stimme.txt").write_text("Hallo, ich bin neu.", encoding="utf-8")
        os.utime(tmp_path / "stimme.wav", (time.time() + 5, time.time() + 5))
        c.post("/stimme/neu")
        c.post("/v1/audio/speech", json={"input": "Zwei"})
        c.post("/v1/audio/speech", json={"input": "Drei"})
        assert geladen == [1] and neu == ["Hallo, ich bin neu."]


def test_qwen3_satz_hat_obergrenze_und_wechselt_bei_endlosschleife(monkeypatch):
    """Ohne max_new_tokens redet Qwen3-TTS bei unpassender Vorlage bis zu 4096 Rahmen weiter."""
    import numpy as np

    import qulsi_tts

    aufrufe = []

    class Modell:
        def generate_voice_clone(self, text, language, voice_clone_prompt, max_new_tokens):
            aufrufe.append((voice_clone_prompt, max_new_tokens))
            sekunden = max_new_tokens / 12 if voice_clone_prompt == "vorlage" else 1.0  # läuft bis zur Grenze
            return [np.zeros(int(24000 * sekunden), dtype=np.float32)], 24000

        def create_voice_clone_prompt(self, ref_audio, x_vector_only_mode):
            assert x_vector_only_mode is True
            return "merkmale"

    q = object.__new__(qulsi_tts.Qwen3TTS)
    q.teile, q.sprecher, q.ref = {"base": Modell()}, [], "stimme.wav"
    q.klone, q.merkmale = {"base": "vorlage"}, {"base": False}
    q.wav, q.hat_base, q.standard = "stimme.wav", True, None
    audio, sr = q.erzeugen("Hallo Sandra, wie geht es dir?", None)
    assert aufrufe[0] == ("vorlage", 12 * 7) and aufrufe[1][0] == "merkmale"
    assert len(audio) / sr == 1.0 and q.merkmale["base"] and q.klone["base"] == "merkmale"
    q.erzeugen("Kurz.", None)
    assert aufrufe[-1] == ("merkmale", 36) and len(aufrufe) == 3


def test_voice_meldet_ausfall_ohne_traceback(monkeypatch):
    import qulsi_voice.main as voice

    monkeypatch.setenv("STT_PROVIDER", "fake")
    monkeypatch.setenv("TTS_PROVIDER", "fake")
    monkeypatch.delenv("TTS_ERSATZ", raising=False)

    class Kaputt:
        async def sprechen(self, text, stimme):
            raise RuntimeError("Grafikspeicher voll")

    with TestClient(voice.app) as c:
        voice.app.state.tts = Kaputt()
        r = c.post("/tts", json={"text": "Hallo"})
    assert r.status_code == 503 and "antwortet gerade nicht" in r.json()["detail"]


def test_vorladen_beim_start(monkeypatch):
    import time

    import qulsi_tts

    monkeypatch.setattr(qulsi_tts, "MODELL", "fake")
    monkeypatch.setenv("TTS_LEERLAUF_SEKUNDEN", "0")
    with TestClient(qulsi_tts.app) as c:
        assert c.get("/health").status_code == 200
        ende = time.monotonic() + 5
        while not c.get("/health").json()["geladen"] and time.monotonic() < ende:
            time.sleep(0.05)
        assert c.get("/health").json()["geladen"] is True
        assert qulsi_tts.app.state.modell.freigeben_wenn_leerlauf() is False  # bleibt geladen


def test_stimmprobe_aufbereiten_entrauscht_und_gleicht_an():
    import numpy as np

    import aufbereiten

    sr = 24000
    t = np.arange(8 * sr) / sr
    huelle = ((np.sin(2 * np.pi * 2.5 * t) > 0.2) & (t > 0.8) & (t < 7.2)).astype(float)
    sprache = sum(np.sin(2 * np.pi * f * t) / (k + 1) for k, f in enumerate([150, 300, 900, 1800])) * huelle * 0.2
    x = sprache + np.random.default_rng(0).normal(0, 0.02, len(t)) + 0.03 * np.sin(2 * np.pi * 50 * t)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())

    def snr(s):
        return 10 * np.log10((s[huelle > 0] ** 2).mean() / (s[huelle == 0] ** 2).mean())

    sauber = aufbereiten.entrauschen(x.astype(np.float32), sr)
    assert snr(sauber) > snr(x) + 8
    y, sr2 = aufbereiten.aufbereiten(buf.getvalue())
    assert sr2 == sr and len(y) < len(x) - sr  # Stille vorn und hinten gekürzt
    assert abs(20 * np.log10(np.sqrt((y ** 2).mean())) + 20) < 0.5 and np.abs(y).max() <= 0.9


def test_qwen3_stimme_waehlen_ohne_neustart():
    """Eigene Stimme und eingebaute Sprecher nebeneinander: die Anfrage sagt, wer spricht."""
    import numpy as np

    import qulsi_tts

    class Modell:
        def __init__(self, name):
            self.name = name

        def generate_custom_voice(self, text, speaker, language, max_new_tokens):
            gesprochen.append(("custom", speaker))
            return [np.zeros(2400, dtype=np.float32)], 24000

        def generate_voice_clone(self, text, language, voice_clone_prompt, max_new_tokens):
            gesprochen.append(("base", None))
            return [np.zeros(2400, dtype=np.float32)], 24000

    gesprochen, geladen = [], []
    q = object.__new__(qulsi_tts.Qwen3TTS)
    q.wav, q.hat_base, q.sprecher, q.standard = "stimme.wav", True, ["ryan", "vivian"], "ryan"
    q.teile, q.klone, q.merkmale = {}, {"base": "merkmale", "base_17b": "merkmale"}, {"base": True, "base_17b": True}

    def teil(name):
        if name not in q.teile:
            geladen.append(name)
            q.teile[name] = Modell(name)
        return q.teile[name]
    q.teil = teil
    q.erzeugen("Hallo.", None)
    q.erzeugen("Hallo.", "Vivian")
    q.erzeugen("Hallo.", "eigene")
    q.erzeugen("Hallo.", "gibtsnicht")
    assert gesprochen == [("base", None), ("custom", "vivian"), ("base", None), ("custom", "ryan")]
    assert geladen == ["base", "custom"]  # jeder Teil einmal, danach bleibt er geladen
    q.wav = None  # ohne Probe (oder ausgeschaltet): eingebaute Sprecher
    assert q.waehlen("eigene") == ("custom", "ryan") and q.waehlen(None) == ("custom", "ryan")


def test_tts_health_zeigt_stimmen(monkeypatch, tmp_path):
    import qulsi_tts

    monkeypatch.setattr(qulsi_tts, "STIMME", tmp_path)
    monkeypatch.setattr(qulsi_tts, "MODELL", "fake")
    assert qulsi_tts.stimmen_info() == {"eigene": False, "sprecher": ["ryan", "vivian"], "standard": "ryan"}
    (tmp_path / "stimme.wav").write_bytes(b"RIFF")
    assert qulsi_tts.stimmen_info()["standard"] == "eigene"
    monkeypatch.setattr(qulsi_tts, "MODELL", "qwen3-tts")
    monkeypatch.setattr(qulsi_tts, "MODELLE", tmp_path)
    (tmp_path / "Qwen3-TTS-12Hz-0.6B-CustomVoice").mkdir()
    (tmp_path / "Qwen3-TTS-12Hz-0.6B-CustomVoice" / "config.json").write_text(
        '{"talker_config": {"spk_id": {"Serena": 1, "Ryan": 2}}}')
    info = qulsi_tts.stimmen_info()
    assert info == {"eigene": False, "sprecher": ["ryan", "serena"], "standard": "ryan"}  # kein Base-Modell
    (tmp_path / "Qwen3-TTS-12Hz-0.6B-Base").mkdir()
    assert qulsi_tts.stimmen_info()["eigene"] is True


def test_cuda_graph_faellt_auf_schleife_zurueck(monkeypatch):
    """Geht die Aufnahme als CUDA-Graph schief, rechnet der Code-Predictor wie bisher weiter."""
    import types

    import schnell

    gerufen = []
    monkeypatch.setattr(schnell, "_generieren", lambda cp, **kw: gerufen.append(kw) or types.SimpleNamespace(
        sequences=types.SimpleNamespace(device=types.SimpleNamespace(type="cpu"))))
    ein = types.SimpleNamespace(device=types.SimpleNamespace(type="cuda"), shape=(1, 2, 8), dtype="bf16")
    g = schnell.Graph(cp=None)

    def kaputt(*_):
        raise RuntimeError("operation not permitted when stream is capturing")
    monkeypatch.setattr(g, "aufnehmen", kaputt)
    g(inputs_embeds=ein, max_new_tokens=15, do_sample=True, top_k=50, top_p=1.0, temperature=0.9)
    g(inputs_embeds=ein, max_new_tokens=15, do_sample=True, top_k=50, top_p=1.0, temperature=0.9)
    assert g.aus is True and len(gerufen) == 2 and g.graphen == {}


def test_chatterbox_eingebaut_und_eigene_stimme(monkeypatch, tmp_path):
    import numpy as np

    import qulsi_tts

    monkeypatch.setattr(qulsi_tts, "STIMME", tmp_path)
    monkeypatch.setattr(qulsi_tts, "MODELL", "chatterbox")
    assert qulsi_tts.stimmen_info() == {"eigene": False, "sprecher": ["chatterbox"], "standard": "chatterbox"}
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes((np.sin(np.arange(24000 * 3) / 7) * 8000).astype("<i2").tobytes())
    (tmp_path / "stimme.wav").write_bytes(buf.getvalue())
    assert qulsi_tts.stimmen_info()["sprecher"] == ["chatterbox", "chatterbox_eigene"]

    class Modell:
        sr = 24000
        conds = "eingebaut"

        def prepare_conditionals(self, pfad, exaggeration):
            assert Path(pfad).read_bytes()[:4] == b"RIFF"
            self.conds = "eigen"

        def generate(self, text, language_id, exaggeration, cfg_weight):
            gesprochen.append((self.conds, language_id))

            class Tensor:  # so wie torch: squeeze(0).float().cpu().numpy()
                def __getattr__(self, name):
                    return (lambda *a: np.zeros(2400, dtype=np.float32)) if name == "numpy" else (lambda *a: self)
            return Tensor()

    from pathlib import Path
    gesprochen = []
    cb = object.__new__(qulsi_tts.Chatterbox)
    cb.m, cb.lebendigkeit, cb.cfg = Modell(), 0.5, 0.5
    cb.eingebaut = cb.m.conds
    cb.neue_stimme()
    cb.erzeugen("Hallo.", "chatterbox")
    cb.erzeugen("Hallo.", "chatterbox_eigene")
    cb.erzeugen("Hallo.", None)
    assert gesprochen == [("eingebaut", "de"), ("eigen", "de"), ("eingebaut", "de")]


def test_voice_schickt_chatterbox_stimmen_an_den_zweiten_dienst(monkeypatch):
    import asyncio

    import httpx
    import qulsi_voice.main as voice

    monkeypatch.setenv("TTS_URL", "http://qwen/v1")
    monkeypatch.setenv("TTS_URL_CHATTERBOX", "http://cb/v1")
    ziele = []

    def handler(request):
        ziele.append((request.url.host, __import__("json").loads(request.content)["model"]))
        return httpx.Response(200, content=b"RIFF", headers={"content-type": "audio/wav"})

    echt = httpx.AsyncClient
    monkeypatch.setattr(voice.httpx, "AsyncClient", lambda **kw: echt(transport=httpx.MockTransport(handler), **kw))
    t = voice.OpenAITTS()
    for stimme in ("ryan", "chatterbox", "chatterbox_eigene", None):
        asyncio.run(t.sprechen("Hallo.", stimme))
    assert ziele == [("qwen", "qwen3-tts"), ("cb", "chatterbox"), ("cb", "chatterbox"), ("qwen", "qwen3-tts")]


def test_qwen3_17b_als_qualitaetsoption(monkeypatch, tmp_path):
    import qulsi_tts

    monkeypatch.setattr(qulsi_tts, "MODELLE", tmp_path)
    monkeypatch.setattr(qulsi_tts, "STIMME", tmp_path)
    monkeypatch.setattr(qulsi_tts, "MODELL", "qwen3-tts")
    for name in ("qwen3-tts", "qwen3-tts-base"):
        qulsi_tts.ordner(name).mkdir()
    q = object.__new__(qulsi_tts.Qwen3TTS)
    q.wav, q.hat_base, q.sprecher, q.standard = "stimme.wav", True, ["ryan", "vivian"], "ryan"
    # 1.7B noch nicht geladen: dieselbe Stimme mit 0.6B
    assert q.waehlen("eigene_17b") == ("base", None) and q.waehlen("ryan_17b") == ("custom", "ryan")
    assert "eigene_17b" not in qulsi_tts.stimmen_info()["sprecher"]
    for name in ("qwen3-tts-17b", "qwen3-tts-17b-base"):
        qulsi_tts.ordner(name).mkdir()
    assert q.waehlen("eigene_17b") == ("base_17b", None) and q.waehlen("Vivian_17b") == ("custom_17b", "vivian")
    assert q.waehlen("eigene") == ("base", None)  # Standard bleibt 0.6B
    (tmp_path / "stimme.wav").write_bytes(b"RIFF")
    info = qulsi_tts.stimmen_info()
    assert info["sprecher"][:len(qulsi_tts.QWEN_SPRECHER)] == qulsi_tts.QWEN_SPRECHER
    assert "eigene_17b" in info["sprecher"] and "ryan_17b" in info["sprecher"] and info["standard"] == "eigene"
