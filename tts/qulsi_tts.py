"""Sprachausgabe auf der eigenen GPU, mit OpenAI-Schnittstelle (/v1/audio/speech).

Ein Image je Modell (Build-Argument MODELL), weil die Modelle unterschiedliche torch- und
transformers-Versionen brauchen:
  qwen3-tts    Qwen3-TTS-12Hz-0.6B (Standard)  eingebaute Sprecher (CustomVoice); mit eigener Stimme das Base-Modell
  cosyvoice3   Fun-CosyVoice3-0.5B               Stimme aus einer Referenzaufnahme
  chatterbox   Chatterbox Multilingual           eingebaute Stimme, oder Referenzaufnahme
  fake         Sinuston (Tests)

Der Voice-Dienst schickt einzelne Sätze, die Web-App spielt Satz für Satz ab, während der nächste
schon erzeugt wird. Das Modell kommt erst bei der ersten Anfrage in den Grafikspeicher und geht
nach TTS_LEERLAUF_SEKUNDEN ohne Nutzung wieder raus.

Eigene Stimme: eine 5 bis 15 Sekunden lange, saubere WAV-Aufnahme als /stimme/stimme.wav und den
gesprochenen Text als /stimme/stimme.txt (Ordner ./config/stimme). Aus der App kommt sie nach einer
Freigabe dorthin (Core, stimmprobe.py), danach ruft der Core POST /stimme/neu auf.

  python qulsi_tts.py laden    lädt die Modelldateien nach /models/tts (einmalig, mit Internet)
  python qulsi_tts.py dienst   startet den Server
"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import io
import json
import logging
import math
import os
import sys
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

from bei_bedarf import BeiBedarf, freigabe_schleife

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tts")

MODELL = os.environ.get("TTS_MODELL", "qwen3-tts")
MODELLE = Path(os.environ.get("TTS_MODELLE", "/models/tts"))
STIMME = Path(os.environ.get("TTS_STIMME_DIR", "/stimme"))
GERAET = os.environ.get("TTS_GERAET", "cuda")

# Hugging-Face-Repos der Modelle (Ordnername unter /models/tts = letzter Teil)
REPOS = {
    "cosyvoice3": os.environ.get("COSYVOICE_REPO", "FunAudioLLM/Fun-CosyVoice3-0.5B-2512"),
    "qwen3-tts": os.environ.get("QWEN_TTS_REPO", "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"),
    "qwen3-tts-base": os.environ.get("QWEN_TTS_BASE_REPO", "Qwen/Qwen3-TTS-12Hz-0.6B-Base"),  # Stimme klonen
    # 1.7B: besserer Klang, etwa doppelt so viel Rechenzeit; nur geladen, wenn es in der App gewählt wird
    "qwen3-tts-17b": os.environ.get("QWEN_TTS_17B_REPO", "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"),
    "qwen3-tts-17b-base": os.environ.get("QWEN_TTS_17B_BASE_REPO", "Qwen/Qwen3-TTS-12Hz-1.7B-Base"),
    "chatterbox": os.environ.get("CHATTERBOX_REPO", "ResembleAI/chatterbox"),
}


def ordner(modell: str) -> Path:
    return MODELLE / REPOS[modell].split("/")[-1]


def referenz() -> tuple[str | None, str]:
    """(Pfad zur Referenzaufnahme oder None, gesprochener Text oder "")."""
    wav, txt = STIMME / "stimme.wav", STIMME / "stimme.txt"
    if not wav.is_file():
        return None, ""
    return str(wav), txt.read_text(encoding="utf-8").strip() if txt.is_file() else ""


class CosyVoice3:
    """Fun-CosyVoice3: Deutsch, Stimme aus Referenzaufnahme (zero-shot)."""

    PROMPT = "You are a helpful assistant.<|endofprompt|>"

    def __init__(self):
        quelle = Path(os.environ.get("COSYVOICE_SRC", "/opt/CosyVoice"))
        sys.path[:0] = [str(quelle), str(quelle / "third_party" / "Matcha-TTS")]
        from cosyvoice.cli.cosyvoice import AutoModel

        self.m = AutoModel(model_dir=str(ordner("cosyvoice3")))
        self.sr = self.m.sample_rate
        self.wav, self.text = referenz()
        if self.wav is None:
            # Ohne eigene Aufnahme: Beispielstimme aus dem CosyVoice-Repo (spricht Deutsch mit leichtem Akzent)
            self.wav = str(quelle / "asset" / "zero_shot_prompt.wav")
            log.warning("Keine Referenzstimme in %s, nehme die Beispielstimme", STIMME)

    def bereit(self, stimme: str | None = None) -> bool:
        return True

    def erzeugen(self, text: str, stimme: str | None) -> tuple[np.ndarray, int]:
        import torch

        if self.text:
            teile = self.m.inference_zero_shot(text, self.PROMPT + self.text, self.wav, stream=False)
        else:
            teile = self.m.inference_cross_lingual(self.PROMPT + text, self.wav, stream=False)
        audio = torch.cat([t["tts_speech"] for t in teile], dim=1)
        return audio.squeeze(0).float().cpu().numpy(), self.sr


EIGENE = "eigene"  # Name der eigenen Stimme in der Sprecher-Auswahl der App
# Sprecher des 0.6B-CustomVoice-Modells, falls config.json (noch) nicht da ist
QWEN_SPRECHER = ["aiden", "dylan", "eric", "ono_anna", "ryan", "serena", "sohee", "uncle_fu", "vivian"]


def eingebaute_sprecher() -> list[str]:
    """Sprecher des CustomVoice-Modells, ohne es zu laden (aus config.json)."""
    try:
        cfg = json.loads((ordner("qwen3-tts") / "config.json").read_text(encoding="utf-8"))
        namen = sorted(str(k).lower() for k in (cfg.get("talker_config", {}).get("spk_id") or {}))
        return namen or QWEN_SPRECHER
    except (OSError, ValueError):
        return QWEN_SPRECHER


GROSS = "_17b"  # Endung der Stimmen mit Qwen3-TTS 1.7B (besserer Klang, langsamer), z. B. "eigene_17b"
# Teil -> Modellordner (REPOS)
TEILE = {"custom": "qwen3-tts", "base": "qwen3-tts-base",
         "custom" + GROSS: "qwen3-tts-17b", "base" + GROSS: "qwen3-tts-17b-base"}


class Qwen3TTS:
    """Qwen3-TTS mit bis zu vier Teilen, die erst bei Bedarf laden und dann geladen bleiben: CustomVoice
    (eingebaute Sprecher) und Base (klont config/stimme/stimme.wav), jeweils als 0.6B (Standard, schnell) und,
    wenn geladen, als 1.7B (besserer Klang, Stimmen mit der Endung _17b). Welcher Teil spricht, sagt jede
    Anfrage (voice = "eigene", "ryan", "eigene_17b" …, aus den App-Einstellungen); ohne Angabe die eigene Stimme,
    wenn es sie gibt. Umschalten braucht so keinen Neustart.

    Schnell genug für Satz-für-Satz-Vorlesen: die Stimmreferenz (Sprecher-Merkmale und Codes der Aufnahme)
    wird einmal je Teil berechnet, der Code-Predictor läuft als CUDA-Graph (schnell.py), und ein kurzer
    Probesatz beim Laden wärmt die GPU auf, damit der erste echte Satz nicht dafür bezahlt.
    """

    def __init__(self):
        self.wav, self.text = referenz()
        self.hat_base = ordner("qwen3-tts-base").is_dir()
        if self.wav and not self.hat_base:
            log.warning("Eigene Stimme da, aber kein Base-Modell (python qulsi_tts.py laden): nehme eingebaute Sprecher")
        self.sprecher = eingebaute_sprecher() if ordner("qwen3-tts").is_dir() else []
        self.standard = (os.environ.get("TTS_SPRECHER") or "").lower() or (self.sprecher[0] if self.sprecher else None)
        self.teile: dict[str, object] = {}
        self.klone: dict[str, object] = {}       # Base-Teil -> Stimmreferenz
        self.merkmale: dict[str, bool] = {}      # Base-Teil -> nur Sprecher-Merkmale (ohne Vorlage)
        self.ref = None
        self.teil(self.waehlen(None)[0])

    def eigene_moeglich(self) -> bool:
        return bool(self.wav and self.hat_base)

    def waehlen(self, stimme: str | None) -> tuple[str, str | None]:
        """(Teil, Sprecher): ("base", None) für die eigene Stimme, sonst ("custom", Sprecher); mit _17b die 1.7B-Teile,
        falls geladen (sonst dieselbe Stimme mit 0.6B)."""
        wunsch = (stimme or "").strip().lower()
        gross = ""
        if wunsch.endswith(GROSS):
            wunsch = wunsch[:-len(GROSS)]
            gross = GROSS if ordner(TEILE["custom" + GROSS if wunsch in self.sprecher else "base" + GROSS]).is_dir() else ""
        if wunsch in self.sprecher:
            return "custom" + gross, wunsch
        if wunsch in ("", EIGENE, "default") and self.eigene_moeglich():
            return "base" + gross, None
        if not self.sprecher:
            raise RuntimeError("Keine eingebauten Sprecher und keine eigene Stimme (config/stimme/stimme.wav)")
        return "custom", self.standard

    def teil(self, name: str):
        """Lädt den Teil beim ersten Gebrauch (ein paar Sekunden), danach bleibt er geladen."""
        if name in self.teile:
            self.teile[name] = self.teile.pop(name)  # zuletzt benutzt nach hinten
            return self.teile[name]
        import torch
        from qwen_tts import Qwen3TTSModel

        # Grafikspeicher: höchstens zwei Teile zugleich (z. B. eigene Stimme 0.6B und 1.7B), sonst den am längsten
        # nicht benutzten freigeben. TTS_QWEN_TEILE=4 hält alle.
        while len(self.teile) >= int(os.environ.get("TTS_QWEN_TEILE", "2")):
            alt = next(iter(self.teile))
            del self.teile[alt]
            self.klone.pop(alt, None)
            gc.collect()  # CUDA-Graph und Modell verweisen aufeinander
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            log.info("Qwen3-TTS (%s) freigegeben, um Grafikspeicher für %s zu haben", alt, name)

        import schnell

        kw = {"device_map": f"{GERAET}:0" if GERAET == "cuda" else GERAET,
              "dtype": torch.bfloat16 if GERAET == "cuda" else torch.float32}
        if attn := os.environ.get("TTS_ATTENTION", "sdpa" if GERAET == "cuda" else ""):
            kw["attn_implementation"] = attn  # flash_attention_2 nur, wenn flash-attn installiert ist
        t = time.monotonic()
        m = Qwen3TTSModel.from_pretrained(str(ordner(TEILE[name])), **kw)
        if os.environ.get("TTS_SCHNELL", "an") != "aus" and schnell.beschleunigen(
                m, graph=os.environ.get("TTS_CUDA_GRAPH", "an") != "aus"):
            log.info("Qwen3-TTS (%s): schneller Code-Predictor aktiv", name)
        self.teile[name] = m
        if name.startswith("base"):
            self._klon_vorbereiten(name, m)
        elif name == "custom":
            self.sprecher = [s.lower() for s in (m.get_supported_speakers() or [])] or self.sprecher
        if os.environ.get("TTS_AUFWAERMEN", "an") != "aus":
            gross = GROSS if name.endswith(GROSS) else ""
            self.erzeugen("Hallo.", (EIGENE if name.startswith("base") else self.standard) + gross)
        log.info("Qwen3-TTS (%s) geladen und aufgewärmt in %.1f s", name, time.monotonic() - t)
        return m

    def _klon_vorbereiten(self, name: str, m) -> None:
        import aufbereiten

        # Nur Sprecher-Merkmale (schneller, kürzerer Prompt) oder zusätzlich die Aufnahme als Vorlage (ähnlicher)
        if self.ref is None or self.ref is self.wav:
            self.ref = self.wav
            if os.environ.get("TTS_STIMME_AUFBEREITEN", "an") != "aus":
                try:  # entrauschen, Stille kürzen, Lautstärke angleichen (aufbereiten.py)
                    self.ref = aufbereiten.aufbereiten(Path(self.wav).read_bytes())
                    log.info("Stimmprobe aufbereitet: %.1f s", len(self.ref[0]) / self.ref[1])
                except Exception as exc:  # noqa: BLE001  dann eben die Aufnahme wie sie ist
                    log.warning("Stimmprobe nicht aufbereitet (%s), nehme sie unverändert", exc)
        # Ohne Text geht nur der Merkmale-Modus (klingt weniger ähnlich); Core lässt den Text deshalb erkennen
        nur = os.environ.get("TTS_KLON", "vorlage") == "merkmale" or not self.text
        self.merkmale[name] = nur
        self.klone[name] = m.create_voice_clone_prompt(ref_audio=self.ref, ref_text=self.text or None,
                                                       x_vector_only_mode=nur)
        log.info("Stimmreferenz für %s berechnet (%s)", name, "nur Merkmale" if nur else "mit Vorlage")

    def bereit(self, stimme: str | None = None) -> bool:
        self.teil(self.waehlen(stimme)[0])
        return True

    def neue_stimme(self) -> None:
        """Neue Stimmprobe: nur die Stimmreferenzen neu berechnen (gut eine Sekunde je Teil), Modelle bleiben geladen."""
        self.wav, self.text = referenz()
        self.ref = None
        for name, m in self.teile.items():
            if name.startswith("base"):
                if self.wav:
                    self._klon_vorbereiten(name, m)
                else:
                    self.klone.pop(name, None)

    def erzeugen(self, text: str, stimme: str | None) -> tuple[np.ndarray, int]:
        t = time.monotonic()
        # Obergrenze aus der Textlänge (12 Rahmen je Sekunde, großzügig ~6 Zeichen je Sekunde). Ohne sie redet das
        # Modell bei einer unpassenden Vorlage bis zu 4096 Rahmen (über fünf Minuten Audio) weiter.
        rahmen = max(36, int(12 * (2 + len(text) / 6)))
        name, sprecher = self.waehlen(stimme)
        m = self.teil(name)
        if name.startswith("custom"):
            wavs, sr = m.generate_custom_voice(text=text, speaker=sprecher, language="German", max_new_tokens=rahmen)
        else:
            wavs, sr = m.generate_voice_clone(text=text, language="German", voice_clone_prompt=self.klone[name],
                                              max_new_tokens=rahmen)
            if len(wavs[0]) / sr >= rahmen / 12 - 0.5 and not self.merkmale.get(name):
                # Grenze erreicht: meist passt stimme.txt nicht genau zur Aufnahme. Ab jetzt nur Sprecher-Merkmale.
                log.warning("Satz lief bis zur Obergrenze; stimme.txt passt wohl nicht zur Aufnahme. "
                            "Nehme ab jetzt nur die Sprecher-Merkmale (TTS_KLON=merkmale)")
                self.merkmale[name] = True
                self.klone[name] = m.create_voice_clone_prompt(ref_audio=self.ref, x_vector_only_mode=True)
                wavs, sr = m.generate_voice_clone(text=text, language="German", voice_clone_prompt=self.klone[name],
                                                  max_new_tokens=rahmen)
        audio = np.asarray(wavs[0], dtype=np.float32)
        dauer = time.monotonic() - t
        import schnell

        rahmen_n, cp_sek = schnell.messung(m)
        je = f", {dauer / rahmen_n * 1000:.0f} ms je Rahmen, davon Code-Predictor {cp_sek / rahmen_n * 1000:.0f}" \
            if rahmen_n else ""
        log.info("%d Zeichen -> %.1f s Audio in %.2f s (Faktor %.2f%s, Stimme %s)", len(text), len(audio) / sr, dauer,
                 dauer / max(len(audio) / sr, 0.01), je, (sprecher or EIGENE) + (GROSS if name.endswith(GROSS) else ""))
        return audio, sr


CHATTERBOX = "chatterbox"                 # eingebaute Stimme von Chatterbox
CHATTERBOX_EIGENE = "chatterbox_eigene"   # eigene Stimmprobe, mit Chatterbox gesprochen


class Chatterbox:
    """Chatterbox Multilingual (MIT-Lizenz): natürlicheres Deutsch als die Qwen3-Sprecher. Eingebaute Stimme
    (conds.pt aus dem Modell) oder die eigene Stimmprobe. Beide Stimmreferenzen werden einmal berechnet und
    je Satz nur ausgetauscht, das Modell bleibt geladen."""

    def __init__(self):
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS

        t = time.monotonic()
        self.m = ChatterboxMultilingualTTS.from_local(ordner("chatterbox"), GERAET)
        self.eingebaut = self.m.conds
        self.lebendigkeit = float(os.environ.get("CHATTERBOX_EXAGGERATION", "0.5"))
        self.cfg = float(os.environ.get("CHATTERBOX_CFG", "0.5"))
        self.eigen = None
        self.neue_stimme()
        if os.environ.get("TTS_AUFWAERMEN", "an") != "aus":
            self.erzeugen("Hallo.", None)
        log.info("Chatterbox geladen und aufgewärmt in %.1f s", time.monotonic() - t)

    def neue_stimme(self) -> None:
        import tempfile

        import aufbereiten

        wav, _ = referenz()
        self.eigen = None
        if not wav:
            return
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            try:  # wie bei Qwen3: entrauscht, Stille gekürzt, Lautstärke angeglichen
                audio, sr = aufbereiten.aufbereiten(Path(wav).read_bytes())
                f.write(als_wav(audio, sr))
                f.flush()
                quelle = f.name
            except Exception as exc:  # noqa: BLE001
                log.warning("Stimmprobe nicht aufbereitet (%s), nehme sie unverändert", exc)
                quelle = wav
            self.m.prepare_conditionals(quelle, exaggeration=self.lebendigkeit)
        self.eigen = self.m.conds
        log.info("Chatterbox: Stimmreferenz aus der eigenen Stimmprobe berechnet")

    def bereit(self, stimme: str | None = None) -> bool:
        return True

    def erzeugen(self, text: str, stimme: str | None) -> tuple[np.ndarray, int]:
        t = time.monotonic()
        eigene = (stimme or "").lower() in (CHATTERBOX_EIGENE, EIGENE) and self.eigen is not None
        conds = self.eigen if eigene else self.eingebaut
        if conds is None:
            raise RuntimeError("Chatterbox: keine eingebaute Stimme (conds.pt) und keine Stimmprobe")
        self.m.conds = conds
        audio = self.m.generate(text, language_id="de", exaggeration=self.lebendigkeit, cfg_weight=self.cfg)
        audio = audio.squeeze(0).float().cpu().numpy()
        dauer = time.monotonic() - t
        log.info("%d Zeichen -> %.1f s Audio in %.2f s (Faktor %.2f, Stimme %s)", len(text), len(audio) / self.m.sr,
                 dauer, dauer / max(len(audio) / self.m.sr, 0.01), CHATTERBOX_EIGENE if eigene else CHATTERBOX)
        return audio, self.m.sr


class Fake:
    """Für Tests: Sinuston, Länge abhängig vom Text."""

    def bereit(self, stimme: str | None = None) -> bool:
        return True

    def erzeugen(self, text: str, stimme: str | None) -> tuple[np.ndarray, int]:
        sr = 24000
        n = int(sr * min(2.0, 0.05 + len(text) / 200))
        return np.array([0.1 * math.sin(2 * math.pi * 440 * i / sr) for i in range(n)], dtype=np.float32), sr


FAKE_SPRECHER = ["ryan", "vivian"]


def stimmen_info() -> dict:
    """Was die App zur Auswahl anbietet, ohne ein Modell zu laden."""
    wav, _ = referenz()
    if MODELL == "qwen3-tts":
        basis = eingebaute_sprecher() if ordner("qwen3-tts").is_dir() else []
        eigene = bool(wav) and ordner("qwen3-tts-base").is_dir()
        standard = (os.environ.get("TTS_SPRECHER") or "").lower() or (basis[0] if basis else None)
        sprecher = list(basis)
        if wav and ordner("qwen3-tts-17b-base").is_dir():  # 1.7B als Qualitätsoption, hinter den 0.6B-Stimmen
            sprecher.append(EIGENE + GROSS)
        if ordner("qwen3-tts-17b").is_dir():
            sprecher += [x + GROSS for x in basis]
    elif MODELL == "chatterbox":  # beide Stimmen als Sprecher, damit die App sie neben Qwen3 anbieten kann
        sprecher = [CHATTERBOX] + ([CHATTERBOX_EIGENE] if wav else [])
        eigene, standard = False, CHATTERBOX
    else:
        sprecher = FAKE_SPRECHER if MODELL == "fake" else []
        eigene, standard = bool(wav), (sprecher[0] if sprecher else None)
    return {"eigene": eigene, "sprecher": sprecher, "standard": EIGENE if eigene else standard}


MODELLKLASSEN = {"cosyvoice3": CosyVoice3, "qwen3-tts": Qwen3TTS, "chatterbox": Chatterbox, "fake": Fake}


def als_wav(audio: np.ndarray, sr: int) -> bytes:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm)
    return buf.getvalue()


WARTEN = float(os.environ.get("TTS_WARTEN_SEKUNDEN", "10"))      # so lange auf eine laufende Erzeugung warten
HOECHSTENS = float(os.environ.get("TTS_HOECHSTENS_SEKUNDEN", "20"))  # dann 504, der Voice-Dienst nimmt Piper
NOTAUS = float(os.environ.get("TTS_NOTAUS_SEKUNDEN", "300"))   # hängt eine Erzeugung so lange: Neustart


_stand_cache: dict = {}


def stimme_stand() -> str | None:
    """Fingerabdruck der eigenen Stimme (Inhalt von stimme.wav und stimme.txt). Ändert er sich, wird die
    Stimmreferenz vor dem nächsten Satz neu berechnet. Nach Inhalt, nicht nach Änderungszeit: auf dem
    Netzspeicher von RunPod kann die sich ändern, ohne dass jemand die Datei anfasst."""
    teile = []
    for name in ("stimme.wav", "stimme.txt"):
        try:
            st = (STIMME / name).stat()
        except OSError:
            teile.append(None)
            continue
        merkmal = (name, st.st_size, st.st_mtime_ns)
        if _stand_cache.get(name, (None,))[0] != merkmal:
            _stand_cache[name] = (merkmal, hashlib.sha1((STIMME / name).read_bytes()).hexdigest())
        teile.append(_stand_cache[name][1])
    return None if teile[0] is None else ":".join(str(t) for t in teile)


async def waechter() -> None:
    """Hängt die GPU-Arbeit (Treiber, Endlosschleife im Modell), beendet sich der Dienst; supervisord bzw.
    Docker starten ihn neu. Sonst bliebe das Lock für immer belegt."""
    while True:
        await asyncio.sleep(10)
        seit = app.state.beschaeftigt_seit
        if seit and time.monotonic() - seit > NOTAUS:
            log.error("Erzeugung hängt seit %.0f s, TTS-Dienst startet neu", time.monotonic() - seit)
            os._exit(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    leerlauf = float(os.environ.get("TTS_LEERLAUF_SEKUNDEN", "600"))
    # 0 = immer geladen. Geladen wird trotzdem erst nach dem Start im Hintergrund, damit /health sofort antwortet
    # und ein Ladefehler den Dienst nicht in eine Neustart-Schleife schickt.
    # TTS_VORLADEN=0: erst beim ersten Satz laden, dann trotzdem nicht mehr freigeben (Chatterbox auf RunPod)
    vorladen = os.environ.get("TTS_VORLADEN", "") == "1" or (leerlauf <= 0 and os.environ.get("TTS_VORLADEN") != "0")
    app.state.modell = await asyncio.to_thread(BeiBedarf, f"TTS {MODELL}", MODELLKLASSEN[MODELL],
                                               leerlauf if leerlauf > 0 else float("inf"))
    app.state.lock = asyncio.Lock()
    app.state.beschaeftigt_seit = None
    app.state.stimme_geladen = None
    app.state.aufgaben = set()
    hintergrund = [asyncio.create_task(freigabe_schleife([app.state.modell])), asyncio.create_task(waechter())]
    log.info("TTS-Dienst gestartet mit %s (pid %d, %s)", MODELL, os.getpid(),
             "bleibt geladen" if leerlauf <= 0 else f"gibt nach {leerlauf:.0f} s Leerlauf frei")
    if vorladen:
        await vorwaermen()
    yield
    for h in hintergrund:
        h.cancel()


app = FastAPI(title="QuLSI TTS", lifespan=lifespan)


@app.get("/health")
async def health():
    seit = app.state.beschaeftigt_seit
    return {"ok": True, "modell": MODELL, "geladen": app.state.modell.geladen,
            "beschaeftigt_sekunden": round(time.monotonic() - seit, 1) if seit else None,
            "stimmen": stimmen_info()}


@app.post("/stimme/neu")
async def stimme_neu():
    """Neue eigene Stimme: wartet nicht auf eine laufende Erzeugung. Die Stimmreferenz wird gleich im Hintergrund
    neu berechnet (oder vor dem nächsten Satz, weil sich stimme_stand() geändert hat); das Modell bleibt geladen."""
    if app.state.modell.geladen:
        await vorwaermen()
    return {"ok": True, "neu_geladen_bei_naechster_anfrage": True}


def neue_stimme_uebernehmen() -> None:
    try:
        app.state.modell.neue_stimme()
        log.info("Neue Stimmprobe übernommen, Modell bleibt geladen")
    except AttributeError:  # Modelle ohne neue_stimme(): neu laden
        log.info("Neue Stimmprobe: lade das Modell neu")
        app.state.modell.freigeben()


async def _mit_lock(arbeit, *args):
    """Arbeit im Thread unter dem Lock. Das Lock gilt, bis der Thread wirklich fertig ist, auch wenn der
    Aufrufer nach HOECHSTENS Sekunden schon mit 504 aufgibt."""
    try:
        await asyncio.wait_for(app.state.lock.acquire(), WARTEN)
    except TimeoutError:
        raise HTTPException(503, "Die Sprachausgabe ist gerade beschäftigt.") from None
    try:
        stand = await asyncio.to_thread(stimme_stand)
        if app.state.modell.geladen and stand != app.state.stimme_geladen:
            await asyncio.to_thread(neue_stimme_uebernehmen)
        if stand != app.state.stimme_geladen or not app.state.modell.geladen:
            app.state.stimme_geladen = stand
        app.state.beschaeftigt_seit = time.monotonic()
        aufgabe = asyncio.ensure_future(asyncio.to_thread(arbeit, *args))
    except BaseException:
        app.state.lock.release()
        raise

    def fertig(_):
        app.state.beschaeftigt_seit = None
        app.state.lock.release()
    aufgabe.add_done_callback(fertig)
    try:
        return await asyncio.wait_for(asyncio.shield(aufgabe), HOECHSTENS)
    except TimeoutError:
        log.warning("Erzeugung dauert länger als %.0f s, Antwort ohne Audio (Piper springt ein)", HOECHSTENS)
        raise HTTPException(504, "Die Sprachausgabe braucht zu lange.") from None


@app.post("/vorwaermen")
async def vorwaermen(body: dict | None = None):
    """Modell (bzw. den Teil für die gewählte Stimme) schon laden: während das Sprachmodell noch die Antwort
    schreibt (Core ruft das bei Spracheingabe) und gleich nach einem Stimmwechsel in der App."""
    stimme = (body or {}).get("stimme") or None
    if not app.state.lock.locked():
        aufgabe = asyncio.create_task(_vorwaermen(stimme))
        app.state.aufgaben.add(aufgabe)
        aufgabe.add_done_callback(app.state.aufgaben.discard)
    return {"ok": True, "geladen": app.state.modell.geladen}


async def _vorwaermen(stimme: str | None = None) -> None:
    try:
        await _mit_lock(app.state.modell.bereit, stimme)
    except Exception as exc:  # noqa: BLE001  die echte Anfrage meldet den Fehler (und Piper springt ein)
        log.warning("Vorwärmen fehlgeschlagen: %s", getattr(exc, "detail", exc))


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": MODELL, "object": "model"}]}


@app.post("/v1/audio/speech")
async def speech(body: dict):
    text = str(body.get("input") or "").strip()
    if not text:
        raise HTTPException(400, "Kein Text")
    stimme = body.get("voice")
    # eine Erzeugung zur Zeit, die GPU teilt sich mit Bonsai
    audio, sr = await _mit_lock(app.state.modell.erzeugen, text[:2000],
                                None if stimme in (None, "", "default") else stimme)
    if body.get("response_format", "wav") == "pcm":
        return Response((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes(), media_type="audio/pcm",
                        headers={"X-Sample-Rate": str(sr)})
    return Response(als_wav(audio, sr), media_type="audio/wav")


def laden(nur: list[str] | None = None) -> None:
    from huggingface_hub import snapshot_download

    if MODELL == "fake":
        return
    muster = None
    if MODELL == "chatterbox":  # nur das mehrsprachige Modell, nicht das englische
        muster = ["ve.pt", "t3_mtl23ls_v2.safetensors", "s3gen.pt", "grapheme_mtl_merged_expanded_v1.json",
                  "conds.pt", "Cangjie5_TC.json"]
    namen = [MODELL]
    if MODELL == "qwen3-tts":
        namen.append("qwen3-tts-base")
        if os.environ.get("TTS_QWEN_17B", "an") != "aus":  # je etwa 4 GB
            namen += ["qwen3-tts-17b", "qwen3-tts-17b-base"]
    for name in nur or namen:
        log.info("Lade %s nach %s", REPOS[name], ordner(name))
        snapshot_download(REPOS[name], local_dir=str(ordner(name)), allow_patterns=muster)


if __name__ == "__main__":
    befehl = sys.argv[1] if len(sys.argv) > 1 else "dienst"
    if befehl == "laden":  # qulsi_tts.py laden [qwen3-tts-17b qwen3-tts-17b-base]: nur diese Modelle
        laden(sys.argv[2:] or None)
    else:
        import uvicorn

        uvicorn.run(app, host=os.environ.get("TTS_HOST", "0.0.0.0"), port=int(os.environ.get("TTS_PORT", "8400")))
