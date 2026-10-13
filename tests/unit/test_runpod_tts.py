"""RunPod: Qwen3-TTS für die eigene Stimme, Piper als Rückfall, Modell nur bei Bedarf im Grafikspeicher."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RP = ROOT / "runpod"


def _tts_an(tmp_path, ok: bool, **env) -> bool:
    venvs = tmp_path / "venvs"
    (venvs / "tts").mkdir(parents=True, exist_ok=True)
    marke = venvs / "tts" / ".tts-ok"
    marke.touch() if ok else marke.unlink(missing_ok=True)
    e = {"PATH": os.environ["PATH"], "WORKSPACE": str(tmp_path), "QULSI_VENVS": str(venvs), **env}
    r = subprocess.run(["bash", "-c", f"source {RP / 'paths.sh'}; tts_an"], env=e, capture_output=True)
    return r.returncode == 0


def test_tts_nur_wenn_eingerichtet_und_nicht_abgeschaltet(tmp_path):
    assert _tts_an(tmp_path, True)
    assert _tts_an(tmp_path, True, TTS_PROVIDER="openai")
    assert not _tts_an(tmp_path, False)
    assert not _tts_an(tmp_path, True, QULSI_DISABLE="vision,tts")
    assert not _tts_an(tmp_path, True, TTS_PROVIDER="piper")


def test_runpod_startet_tts_lokal_mit_piper_als_rueckfall():
    run = (RP / "run.sh").read_text(encoding="utf-8")
    assert "TTS_HOST=127.0.0.1" in run and "TTS_STIMME_DIR=$APP/config/stimme" in run
    assert "TTS_PROVIDER=openai TTS_URL=http://127.0.0.1:$PORT_TTS/v1 TTS_MODEL=qwen3-tts TTS_ERSATZ=piper" in run
    assert 'TTS_DIENST_URL=$(tts_an && echo "http://127.0.0.1:$PORT_TTS")' in run  # Core lädt nach neuer Stimme neu
    start = (RP / "start.sh").read_text(encoding="utf-8")
    assert 'tts_an && DIENSTE="$DIENSTE tts:30"' in start and "[tts]=$PORT_TTS" in start
    assert "TTS_PROVIDER=openai" in start and "tts-umgestellt" in start
    setup = (RP / "setup.sh").read_text(encoding="utf-8")
    assert "qwen-tts==0.1.1" in setup and "transformers==4.57.3" in setup and "qulsi_tts.py laden" in setup
    for f in ("run.sh", "start.sh", "setup.sh", "paths.sh"):
        assert subprocess.run(["bash", "-n", str(RP / f)]).returncode == 0


def test_versionen_wie_im_docker_image():
    docker = (ROOT / "tts" / "Dockerfile").read_text(encoding="utf-8")
    setup = (RP / "setup.sh").read_text(encoding="utf-8")
    for paket in ("qwen-tts==0.1.1", "transformers==4.57.3", "accelerate==1.12.0", "fastapi==0.118.0"):
        assert paket in docker and paket in setup, paket


def test_hinweis_auf_runpod(monkeypatch):
    import asyncio

    import httpx

    from qulsi_core import stimmprobe

    class _Antwort:
        def json(self):
            return {"tts": "piper", "tts_modell": None}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Antwort()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setenv("QULSI_LAUFZEIT", "runpod")
    f = asyncio.run(stimmprobe.faehigkeit("http://voice"))
    assert f["klonen"] is False and "start.sh" in f["hinweis"] and "nur auf der Box" not in f["hinweis"]


def test_chatterbox_als_zweiter_dienst(tmp_path):
    venvs = tmp_path / "venvs"
    for v in ("tts", "tts-chatterbox"):
        (venvs / v).mkdir(parents=True)
        (venvs / v / ".tts-ok").write_text("")

    def an(**env):
        e = {**os.environ, "QULSI_VENVS": str(venvs), "WORKSPACE": str(tmp_path), **env}
        return subprocess.run(["bash", "-c", f"source {RP / 'paths.sh'}; tts_cb_an"], env=e).returncode == 0
    assert an() and not an(QULSI_DISABLE="tts-chatterbox") and not an(TTS_PROVIDER="piper")
    run = (RP / "run.sh").read_text(encoding="utf-8")
    assert "tts_cb_an && export TTS_URL_CHATTERBOX=http://127.0.0.1:$PORT_TTS_CB/v1" in run
    assert 'exec "$CB_VENV/bin/python" qulsi_tts.py dienst' in run and "TTS_MODELL=chatterbox" in run
    assert 'TTS_CHATTERBOX_DIENST_URL=$(tts_cb_an && echo "http://127.0.0.1:$PORT_TTS_CB")' in run
    start = (RP / "start.sh").read_text(encoding="utf-8")
    assert 'tts_cb_an && DIENSTE="$DIENSTE tts-chatterbox:30"' in start and "[tts-chatterbox]=$PORT_TTS_CB" in start
    setup = (RP / "setup.sh").read_text(encoding="utf-8")
    assert "--no-deps chatterbox-tts==0.1.7" in setup and "transformers==5.2.0" in setup
    assert ".chatterbox-ok" in setup and "TTS_MODELL=chatterbox" in setup
