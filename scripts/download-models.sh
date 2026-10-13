#!/usr/bin/env bash
# Lädt alle Modelldateien nach ./models (bzw. MODELS_DIR). Schon vorhandene werden übersprungen.
#   Bonsai-27B (PQ2_0, 7,2 GB), optional dessen Bildteil (mmproj)
#   kleines Modell Qwen3-1.7B (1,8 GB), Embedding-Modell Qwen3-Embedding-0.6B (0,6 GB)
#   Whisper large-v3-turbo (1,6 GB) und die deutsche Piper-Stimme (0,1 GB)
# Dateinamen vorher auf den Modellseiten prüfen, z. B.
#   https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && { set -a; source .env; set +a; }
M=${MODELS_DIR:-models}
PY=${PYTHON:-python3}
mkdir -p "$M/voice"
$PY -c "import huggingface_hub" 2>/dev/null || $PY -m pip install -q huggingface_hub

laden() {  # laden <repo> <datei> <zielordner>
  if [ -f "$3/$(basename "$2")" ]; then echo "vorhanden: $2"; return; fi
  echo "== $1 / $2"
  $PY - "$1" "$2" "$3" <<'PY'
import shutil, sys
from pathlib import Path
from huggingface_hub import hf_hub_download
pfad = hf_hub_download(sys.argv[1], sys.argv[2], local_dir=sys.argv[3])
ziel = Path(sys.argv[3]) / Path(sys.argv[2]).name
if Path(pfad) != ziel:  # Dateien aus Unterordnern flach ablegen
    shutil.move(pfad, ziel)
PY
}

BONSAI_REPO=${BONSAI_REPO:-prism-ml/Ternary-Bonsai-2-27B-gguf}

mmproj_suchen() {  # Bildteil (mmproj) im Modell-Repo finden: gibt den Dateinamen aus oder nichts
  $PY - "$BONSAI_REPO" <<'PY'
import sys
from huggingface_hub import list_repo_files
try:
    dateien = [d for d in list_repo_files(sys.argv[1]) if "mmproj" in d.lower() and d.endswith(".gguf")]
except Exception as exc:  # noqa: BLE001
    print(f"mmproj-Suche fehlgeschlagen: {exc}", file=sys.stderr)
    dateien = []
rang = lambda d: next((i for i, t in enumerate(("f16", "bf16", "q8")) if t in d.lower()), 9)  # noqa: E731
print(sorted(dateien, key=rang)[0] if dateien else "")
PY
}

# Nur den Bildteil holen: scripts/download-models.sh mmproj  (letzte Zeile = Dateiname oder leer)
if [ "${1:-}" = "mmproj" ]; then
  name=${BONSAI_MMPROJ:-$(mmproj_suchen)}
  [ -n "$name" ] && laden "$BONSAI_REPO" "$name" "$M" >&2
  echo "$(basename "${name:-}")"
  exit 0
fi

laden "$BONSAI_REPO" "${BONSAI_FILE:-Ternary-Bonsai-2-27B-PQ2_0.gguf}" "$M"
BONSAI_MMPROJ=${BONSAI_MMPROJ:-$(mmproj_suchen)}
if [ -n "$BONSAI_MMPROJ" ]; then
  laden "$BONSAI_REPO" "$BONSAI_MMPROJ" "$M"
  echo "Bildteil: BONSAI_MMPROJ=$(basename "$BONSAI_MMPROJ")  (in die .env eintragen, falls noch leer)"
else
  echo "Hinweis: Im Repo $BONSAI_REPO gibt es keinen Bildteil (mmproj). Fragen zu Bildern sind dann nicht verfügbar."
fi
if [ "${MODEL_SMALL:-}" != "aus" ]; then
  laden "${KLEIN_REPO:-Qwen/Qwen3-1.7B-GGUF}" "${KLEIN_FILE:-Qwen3-1.7B-Q8_0.gguf}" "$M"
fi
laden "${EMBED_REPO:-Qwen/Qwen3-Embedding-0.6B-GGUF}" "${EMBED_FILE:-Qwen3-Embedding-0.6B-Q8_0.gguf}" "$M"

# Sprache: Whisper als ganzer Ordner, Piper-Stimme als zwei Dateien
STT_MODEL=${STT_MODEL:-large-v3-turbo}
if [ ! -d "$M/voice/faster-whisper-$STT_MODEL" ]; then
  echo "== Whisper $STT_MODEL"
  $PY -c "import sys; from huggingface_hub import snapshot_download; snapshot_download(sys.argv[1], local_dir=sys.argv[2])" \
    "${STT_REPO:-mobiuslabsgmbh/faster-whisper-$STT_MODEL}" "$M/voice/faster-whisper-$STT_MODEL"
fi
STIMME=${TTS_VOICE:-de_DE-thorsten-high}
IFS=_- read -r SPR LAND NAME QUAL <<< "$STIMME"
for f in "$STIMME.onnx" "$STIMME.onnx.json"; do
  laden rhasspy/piper-voices "${SPR}/${SPR}_${LAND}/${NAME}/${QUAL}/$f" "$M/voice"
done

ls -lh "$M"/*.gguf "$M/voice" 2>/dev/null
