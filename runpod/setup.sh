#!/usr/bin/env bash
# Einrichtung auf einem RunPod-GPU-Pod (ein Container, kein Docker Compose).
# Wird von runpod/start.sh automatisch aufgerufen. Jeder Schritt ist wiederholbar:
# Was schon auf dem Volume /workspace liegt, wird übersprungen.
#
# Systempakete (Postgres) liegen auf der Container-Disk und sind nach einem Neustart
# des Pods weg. Sie werden dann in etwa einer Minute neu installiert.
#
# QULSI_SKIP: kommagetrennte Liste von Schritten, die ausgelassen werden
#   system, llm-build, models, webui, ml, litellm, ntfy, qdrant, tts
set -euo pipefail
source "$(dirname "$0")/paths.sh"
# /workspace kann Netzspeicher ohne Dateibesitzer sein (RunPod EU): Entpacken immer mit
# --no-same-owner, kein chown auf dem Volume.

skip() { [[ ",${QULSI_SKIP:-}," == *",$1,"* ]]; }
log() { echo -e "\n== $*"; }

mkdir -p "$DATA" "$MODELS/voice" "$BIN" "$DATA/objects" "$DATA/qdrant" "$DATA/ntfy" "$DATA/files" "$DATA/fotos" "$DATA/memchip" "$DATA/open-webui"

# ---------------------------------------------------------------- Systempakete
if ! skip system && [ ! -x /usr/lib/postgresql/16/bin/postgres ]; then
  log "Postgres 16 installieren"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq curl ca-certificates gnupg lsb-release git cmake build-essential libssl-dev >/dev/null
  install -d /usr/share/postgresql-common/pgdg
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  apt-get update -qq
  # Nur die Binärdateien, ohne den automatisch angelegten Cluster
  apt-get install -y -qq --no-install-recommends postgresql-16 postgresql-client-16 >/dev/null
  pg_dropcluster --stop 16 main 2>/dev/null || true
fi

# ---------------------------------------------------------------- Python-Umgebungen
# Installiert wird mit uv (deutlich schneller als pip) auf die Container-Disk ($VENV_ROOT).
# Danach kommt je Umgebung ein Archiv nach $VENV_ARCHIV auf das Volume; fehlt die Umgebung nach
# einem Neustart des Pods, wird sie daraus ausgepackt statt neu installiert.
PY=${PYTHON:-python3}
mkdir -p "$VENV_ROOT" "$VENV_ARCHIV"
if ! command -v uv >/dev/null; then
  log "uv installieren"
  $PY -m pip install -q uv 2>/dev/null || $PY -m pip install -q --break-system-packages uv 2>/dev/null || true
fi
export UV_CACHE_DIR=${UV_CACHE_DIR:-/var/cache/qulsi-uv} UV_LINK_MODE=copy
installieren() {  # installieren <venv> <pip-Argumente...>
  local v=$1; shift
  if command -v uv >/dev/null; then uv pip install -q --python "$v/bin/python" "$@"
  else "$v/bin/pip" install -q "$@"; fi
}
if command -v zstd >/dev/null; then PACK=(-I "zstd -T0 -3"); ENDUNG=tar.zst; else PACK=(); ENDUNG=tar; fi
archiv() { echo "$VENV_ARCHIV/$(basename "$1").$ENDUNG"; }
auspacken() {  # Umgebung aus dem Archiv holen, wenn sie auf der Container-Disk fehlt
  local v=$1 a; a=$(archiv "$1")
  if [ ! -x "$v/bin/python" ] && [ -f "$a" ]; then
    log "Umgebung $(basename "$v") aus dem Archiv auspacken"
    tar "${PACK[@]}" --no-same-owner -xf "$a" -C "$VENV_ROOT" || rm -rf "$v"
  fi
}
einpacken() {  # Archiv neu schreiben (erst .tmp, dann umbenennen)
  local v=$1 a; a=$(archiv "$1")
  log "Umgebung $(basename "$v") auf das Volume sichern"
  tar "${PACK[@]}" -cf "$a.tmp" -C "$VENV_ROOT" "$(basename "$v")" && mv "$a.tmp" "$a"
}

auspacken "$VENV"
if [ ! -x "$VENV/bin/python" ]; then
  log "Python-Umgebung für QuLSI"
  # --system-site-packages: torch aus dem RunPod-PyTorch-Image mitbenutzen (CUDA passt dann sicher)
  $PY -m venv --system-site-packages "$VENV"
fi
GEAENDERT=0
REQS=("$APP/core/requirements.txt" "$APP/connectors/requirements.txt" "$APP/vision/requirements.txt" "$APP/voice/requirements.txt"
      "$APP/tresor/requirements.txt")
NEU=0; for r in "${REQS[@]}"; do [ "$r" -nt "$VENV/.qulsi-ok" ] && NEU=1; done
if [ ! -f "$VENV/.qulsi-ok" ] || [ $NEU = 1 ]; then
  log "Pakete für QuLSI"
  installieren "$VENV" $(printf -- "-r %s " "${REQS[@]}") pyyaml supervisor huggingface_hub
  touch "$VENV/.qulsi-ok"; GEAENDERT=1
fi
if ! skip ml && [ ! -f "$VENV/.ml-ok" ]; then
  log "Bild- und Sprachmodelle (transformers, faster-whisper, piper)"
  "$VENV/bin/python" -c "import torch; print('torch', torch.__version__, 'CUDA', torch.cuda.is_available())" \
    || installieren "$VENV" torch torchvision --index-url https://download.pytorch.org/whl/cu128
  installieren "$VENV" -r "$APP/vision/requirements-ml.txt" -r "$APP/voice/requirements-ml.txt"
  touch "$VENV/.ml-ok"; GEAENDERT=1
fi
# YOLO-World nur auf Wunsch (DETECTOR=yolo-world in $ENV_FILE): ultralytics steht unter AGPL-3.0
DETEKTOR=$(grep -E '^DETECTOR=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2 | cut -d'#' -f1 | tr -d ' ' || true)
if ! skip ml && [ "${DETEKTOR:-owlv2}" = yolo-world ] && [ ! -f "$VENV/.yolo-ok" ]; then
  log "YOLO-World (ultralytics, AGPL-3.0), weil DETECTOR=yolo-world"
  installieren "$VENV" -r "$APP/vision/requirements-yolo.txt"
  touch "$VENV/.yolo-ok"; GEAENDERT=1
fi
[ $GEAENDERT = 1 ] && einpacken "$VENV"

# LiteLLM in eigener Umgebung, weil es eigene feste Versionen von fastapi usw. mitbringt
if ! skip litellm; then
  auspacken "$LITELLM_VENV"
  if [ ! -x "$LITELLM_VENV/bin/litellm" ]; then
    log "LiteLLM"
    $PY -m venv "$LITELLM_VENV"
    installieren "$LITELLM_VENV" "litellm[proxy]"
    einpacken "$LITELLM_VENV"
  fi
fi

# Qwen3-TTS (eigene Stimme aus der App) in eigener Umgebung: braucht feste transformers-Versionen.
# Schlägt das fehl, läuft die Sprachausgabe weiter mit Piper (start.sh startet den Dienst dann nicht).
tts_einrichten() {
  auspacken "$TTS_VENV"
  [ -f "$TTS_VENV/.tts-ok" ] && [ ! "$APP/tts/Dockerfile" -nt "$TTS_VENV/.tts-ok" ] && return 0
  log "Qwen3-TTS für die eigene Stimme (einmalig, ein paar Minuten)"
  command -v sox >/dev/null || { export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends sox libsndfile1 ffmpeg >/dev/null; }
  [ -x "$TTS_VENV/bin/python" ] || $PY -m venv "$TTS_VENV"
  # Versionen wie im Docker-Image (tts/Dockerfile): torch 2.8 mit CUDA 12.8 (Blackwell), fest, damit
  # accelerate kein neueres torch nachzieht, das der Treiber des Pods nicht kann
  installieren "$TTS_VENV" torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
  installieren "$TTS_VENV" fastapi==0.118.0 uvicorn==0.37.0 numpy huggingface_hub
  installieren "$TTS_VENV" --no-deps qwen-tts==0.1.1
  installieren "$TTS_VENV" torch==2.8.0 torchaudio==2.8.0 transformers==4.57.3 accelerate==1.12.0 librosa soundfile sox \
    onnxruntime einops
  "$TTS_VENV/bin/python" -c "import qwen_tts, torch" && touch "$TTS_VENV/.tts-ok" && einpacken "$TTS_VENV"
}
if ! skip tts && [[ ",${QULSI_DISABLE:-}," != *",tts,"* ]]; then
  tts_einrichten || echo "Warnung: Qwen3-TTS konnte nicht eingerichtet werden, die Sprachausgabe bleibt bei Piper." >&2
fi

# Chatterbox Multilingual (MIT-Lizenz) zum Vergleich: verlangt torch 2.6, das kann Blackwell nicht. Deshalb
# torch 2.8 wie bei Qwen3 und chatterbox-tts ohne Abhängigkeiten, die übrigen Pakete wie in tts/Dockerfile.
chatterbox_einrichten() {
  auspacken "$CB_VENV"
  [ -f "$CB_VENV/.tts-ok" ] && [ ! "$APP/tts/Dockerfile" -nt "$CB_VENV/.tts-ok" ] && return 0
  log "Chatterbox für natürlicheres Deutsch (einmalig, ein paar Minuten)"
  [ -x "$CB_VENV/bin/python" ] || $PY -m venv "$CB_VENV"
  installieren "$CB_VENV" torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
  installieren "$CB_VENV" fastapi==0.118.0 uvicorn==0.37.0 huggingface_hub
  installieren "$CB_VENV" --no-deps chatterbox-tts==0.1.7
  installieren "$CB_VENV" torch==2.8.0 torchaudio==2.8.0 "numpy<2" librosa==0.11.0 s3tokenizer transformers==5.2.0 \
    diffusers==0.29.0 resemble-perth conformer==0.3.2 safetensors==0.5.3 spacy-pkuseg pykakasi==2.3.0 pyloudnorm omegaconf
  "$CB_VENV/bin/python" -c "import chatterbox.mtl_tts, torch" && touch "$CB_VENV/.tts-ok" && einpacken "$CB_VENV"
}
if ! skip tts && [ -f "$TTS_VENV/.tts-ok" ] && [[ ",${QULSI_DISABLE:-}," != *",tts-chatterbox,"* ]]; then
  chatterbox_einrichten || echo "Warnung: Chatterbox konnte nicht eingerichtet werden, Qwen3-TTS bleibt." >&2
fi

if ! skip webui && [[ ",${QULSI_ENABLE:-}," == *",open-webui,"* ]]; then
  auspacken "$WEBUI_VENV"
  if [ ! -x "$WEBUI_VENV/bin/open-webui" ]; then
    log "Open WebUI (dauert ein paar Minuten)"
    $PY -m venv "$WEBUI_VENV"
    installieren "$WEBUI_VENV" open-webui
    einpacken "$WEBUI_VENV"
  fi
fi

# ---------------------------------------------------------------- Qdrant (Vektorsuche)
QDRANT_VERSION=1.19.2
if ! skip qdrant && [ ! -x "$BIN/qdrant" ]; then
  log "Qdrant $QDRANT_VERSION"
  curl -fsSL "https://github.com/qdrant/qdrant/releases/download/v${QDRANT_VERSION}/qdrant-x86_64-unknown-linux-musl.tar.gz" \
    | tar -xz --no-same-owner -C "$BIN" qdrant
fi

# ---------------------------------------------------------------- ntfy (Handy-Freigaben)
NTFY_VERSION=2.28.0
if ! skip ntfy && [ ! -x "$BIN/ntfy" ]; then
  log "ntfy $NTFY_VERSION"
  curl -fsSL "https://github.com/binwiederhier/ntfy/releases/download/v${NTFY_VERSION}/ntfy_${NTFY_VERSION}_linux_amd64.tar.gz" \
    | tar -xz --no-same-owner -C /tmp
  cp "/tmp/ntfy_${NTFY_VERSION}_linux_amd64/ntfy" "$BIN/ntfy"
fi

# ---------------------------------------------------------------- Bonsai-Server (PrismML-Fork)
if ! skip llm-build && [ ! -x "$BIN/llama-server" ]; then
  log "PrismML-llama.cpp mit CUDA bauen (5 bis 15 Minuten)"
  command -v nvcc >/dev/null || export PATH=/usr/local/cuda/bin:$PATH
  command -v nvcc >/dev/null || { echo "nvcc fehlt: ein RunPod-PyTorch-Template in der devel-Variante wählen" >&2; exit 1; }
  # Rechenarchitektur der Karte im Pod, z. B. 8.6 -> 86
  ARCH=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | tr -d '.')
  # Bauen auf der Container-Disk: auf dem Netzspeicher wären die vielen kleinen Dateien sehr langsam
  SRC=/var/tmp/llama.cpp-prism
  [ -d "$SRC" ] || git clone --depth 50 --branch "${LLAMA_REF:-prism}" https://github.com/PrismML-Eng/llama.cpp "$SRC"
  cmake -S "$SRC" -B "$SRC/build" -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
    -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCHS:-$ARCH}" >/dev/null
  cmake --build "$SRC/build" -j"$(nproc)" --target llama-server
  cp "$SRC/build/bin/llama-server" "$BIN/llama-server"
  git -C "$SRC" rev-parse HEAD > "$BIN/llama-server.commit"
fi

# ---------------------------------------------------------------- Modelle
if ! skip models; then
  set -a; source "$ENV_FILE"; set +a
  MODELS_DIR=$MODELS PYTHON="$VENV/bin/python" bash "$APP/scripts/download-models.sh"
  # Qwen3-TTS: eingebaute Sprecher (CustomVoice) und das Base-Modell zum Stimmklonen, je etwa 2 GB
  if [ -f "$TTS_VENV/.tts-ok" ] && [ ! -f "$MODELS/tts/.qwen3-tts-ok" ]; then
    log "Qwen3-TTS-Modelle laden"
    ( cd "$APP/tts" && TTS_MODELL=qwen3-tts TTS_MODELLE=$MODELS/tts HF_HOME=$MODELS/hf \
        "$TTS_VENV/bin/python" qulsi_tts.py laden qwen3-tts qwen3-tts-base ) && touch "$MODELS/tts/.qwen3-tts-ok" \
      || echo "Warnung: Qwen3-TTS-Modelle nicht geladen, die Sprachausgabe bleibt bei Piper." >&2
  fi
  # Qwen3-TTS 1.7B als Qualitätsoption (je etwa 4 GB), eigener Schritt: ältere Pods haben die 0.6B-Modelle schon.
  # Ohne: TTS_QWEN_17B=aus in die env-Datei.
  if [ -f "$TTS_VENV/.tts-ok" ] && [ ! -f "$MODELS/tts/.qwen3-tts-17b-ok" ] && [ "${TTS_QWEN_17B:-an}" != aus ]; then
    log "Qwen3-TTS 1.7B laden (besserer Klang)"
    ( cd "$APP/tts" && TTS_MODELL=qwen3-tts TTS_MODELLE=$MODELS/tts HF_HOME=$MODELS/hf \
        "$TTS_VENV/bin/python" qulsi_tts.py laden qwen3-tts-17b qwen3-tts-17b-base ) && touch "$MODELS/tts/.qwen3-tts-17b-ok" \
      || echo "Warnung: Qwen3-TTS 1.7B nicht geladen, es bleibt bei 0.6B." >&2
  fi
  # Chatterbox Multilingual, etwa 3 GB
  if [ -f "$CB_VENV/.tts-ok" ] && [ ! -f "$MODELS/tts/.chatterbox-ok" ]; then
    log "Chatterbox-Modell laden"
    ( cd "$APP/tts" && TTS_MODELL=chatterbox TTS_MODELLE=$MODELS/tts HF_HOME=$MODELS/hf \
        "$CB_VENV/bin/python" qulsi_tts.py laden ) && touch "$MODELS/tts/.chatterbox-ok" \
      || echo "Warnung: Chatterbox-Modell nicht geladen, Qwen3-TTS bleibt." >&2
  fi
fi

log "Einrichtung fertig"
