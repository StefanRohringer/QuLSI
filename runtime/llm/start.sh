#!/bin/sh
# Startet llama-server. Ein Image, drei Rollen (LLM_ROLLE):
#   bonsai  Bonsai-27B (Standard)             -> Alias "bonsai"
#   klein   kleines Modell für kurze Befehle  -> Alias "qulsi-klein"
#   embed   Embedding-Modell fürs Gedächtnis  -> Alias "embed"
# Alle Werte kommen aus der .env (siehe .env.example).
#
# Grafikspeicher: Bonsai bleibt geladen. Das kleine Modell und das Embedding-Modell schlafen nach
# *_SCHLAF_SEKUNDEN ohne Anfrage (Modell und KV-Cache raus aus dem Grafikspeicher) und laden bei der
# nächsten Anfrage automatisch neu. -1 = immer geladen.
set -e
MODEL_DIR="${MODEL_DIR:-/models}"
ROLLE="${LLM_ROLLE:-bonsai}"
BIN="${LLM_BIN:-llama-server}"
HOST="${LLM_HOST:-0.0.0.0}"

pruefen() {
  if [ ! -f "$1" ]; then
    echo "Modell fehlt: $1  ->  scripts/download-models.sh ausführen" >&2
    exit 1
  fi
}

case "$ROLLE" in
  klein)
    MODEL="$MODEL_DIR/${KLEIN_FILE:-Qwen3-1.7B-Q8_0.gguf}"; pruefen "$MODEL"
    exec "$BIN" -m "$MODEL" --host "$HOST" --port "${LLM_PORT:-8082}" --alias qulsi-klein \
      --jinja -ngl "${KLEIN_GPU_LAYERS:-99}" -fa on -c "${KLEIN_CTX:-16384}" -np 1 \
      --sleep-idle-seconds "${KLEIN_SCHLAF_SEKUNDEN:-600}" \
      --temp 0.7 --top-p 0.8 --top-k 20 $KLEIN_EXTRA_ARGS
    ;;
  embed)
    MODEL="$MODEL_DIR/${EMBED_FILE:-Qwen3-Embedding-0.6B-Q8_0.gguf}"; pruefen "$MODEL"
    exec "$BIN" -m "$MODEL" --host "$HOST" --port "${LLM_PORT:-8081}" --alias embed \
      --embedding --pooling last -ngl "${EMBED_GPU_LAYERS:-99}" -c 8192 -ub 8192 \
      --sleep-idle-seconds "${EMBED_SCHLAF_SEKUNDEN:-600}" $EMBED_EXTRA_ARGS
    ;;
esac

MODEL="$MODEL_DIR/${BONSAI_FILE:-Ternary-Bonsai-2-27B-PQ2_0.gguf}"; pruefen "$MODEL"
MMPROJ=""
if [ -n "$BONSAI_MMPROJ" ] && [ -f "$MODEL_DIR/$BONSAI_MMPROJ" ]; then
  MMPROJ="--mmproj $MODEL_DIR/$BONSAI_MMPROJ"
fi
# --jinja: Chat-Template des Modells, nötig für Werkzeugaufrufe und enable_thinking
# -np 2:   zwei gleichzeitige Anfragen (Chat und Hintergrund), Kontext wird geteilt
# -ctk/-ctv q4_0: KV-Cache (Kontext) in 4 Bit, halb so groß wie q8_0; braucht -fa on
exec "$BIN" -m "$MODEL" $MMPROJ \
  --host "$HOST" --port "${LLM_PORT:-8080}" --alias bonsai \
  --jinja -ngl "${BONSAI_GPU_LAYERS:-99}" -fa on \
  -c "${BONSAI_CTX:-65536}" -np "${BONSAI_PARALLEL:-2}" \
  -ctk "${BONSAI_KV:-q4_0}" -ctv "${BONSAI_KV:-q4_0}" \
  --sleep-idle-seconds "${BONSAI_SCHLAF_SEKUNDEN:--1}" \
  --temp 0.7 --top-p 0.8 --top-k 20 \
  --metrics $BONSAI_EXTRA_ARGS
