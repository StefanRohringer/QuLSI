#!/usr/bin/env bash
# Startbefehl je Dienst, wird von supervisord aufgerufen:  run.sh <dienst>
# Alle Dienste außer Core (Web-App) und ntfy lauschen nur auf 127.0.0.1,
# weil RunPod jeden freigegebenen Port öffentlich über seinen Proxy erreichbar macht.
#
# Sicherheitszonen: Auf RunPod läuft alles in einem Container. Die Connectoren gehen
# über den Egress-Proxy (HTTPS_PROXY mit ihrem Namen), der nur Manifest-Ziele durchlässt.
# Anders als mit Docker auf der Box ist das hier nicht erzwungen: ein Prozess im Pod könnte
# den Proxy umgehen. Für den Test reicht das; die echte Trennung macht docker-compose.yml.
set -euo pipefail
source "$(dirname "$0")/paths.sh"
set -a; source "$ENV_FILE"; set +a

# QULSI_DB_HOST: vorhandene Datenbank (mit Schema und Rollen) statt der eigenen im Pod
DB_URL() { echo "postgresql://$1:$2@${QULSI_DB_HOST:-127.0.0.1:5432}/qulsi"; }
DATABASE_URL=$(DB_URL qulsi "$POSTGRES_PASSWORD")
LLM_BASE="http://127.0.0.1:4000/v1"
PY="$VENV/bin/python"
export TZ OBJECT_STORE_DIR=$DATA/objects QDRANT_URL=http://127.0.0.1:6333

fake_llm() {  # nur für Tests ohne GPU (QULSI_FAKE_LLM=1)
  exec "$PY" -m uvicorn server:app --app-dir "$APP/runtime/fake-llm" --host 127.0.0.1 --port "$1"
}

llama() {  # llama() <rolle> <port>
  [ -n "${QULSI_FAKE_LLM:-}" ] && fake_llm "$2"
  export MODEL_DIR=$MODELS LLM_BIN=$BIN/llama-server LLM_HOST=127.0.0.1 LLM_ROLLE=$1 LLM_PORT=$2
  exec sh "$APP/runtime/llm/start.sh"   # über sh: im Paket kann das Ausführungsrecht fehlen
}

cuda_libs() {  # faster-whisper (CTranslate2) findet cuBLAS/cuDNN aus den pip-Paketen von torch
  local site
  site=$("$PY" -c "import site; print(site.getsitepackages()[0])" 2>/dev/null || true)
  for p in "$site"/nvidia/cublas/lib "$site"/nvidia/cudnn/lib /usr/local/lib/python3*/dist-packages/nvidia/c*/lib; do
    [ -d "$p" ] && LD_LIBRARY_PATH="$p:${LD_LIBRARY_PATH:-}"
  done
  export LD_LIBRARY_PATH
}

connector() {  # connector <name>
  local name=$1
  local proxy="http://$name:x@127.0.0.1:3128"
  unset ALL_PROXY all_proxy
  [ -n "$TRESOR_LOKAL" ] && export TRESOR_URL=$TRESOR_LOKAL CONNECTOR_NAME=$name   # Geheimnisse aus dem Tresor
  export PORT=${CONNECTOR_PORT[$name]} HOST=127.0.0.1 PYTHONPATH=$APP/connectors/_lib \
         HTTPS_PROXY=$proxy HTTP_PROXY=$proxy https_proxy=$proxy http_proxy=$proxy \
         NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost MCP_ALLOWED_HOSTS="127.0.0.1:*,localhost:*"
  case "$name" in
    calendar) export DATABASE_URL=$(DB_URL qulsi_calendar "$DB_PW_CALENDAR") ;;
    mail)     export DATABASE_URL=$(DB_URL qulsi_mail "$DB_PW_MAIL") ;;
    files)    export DATABASE_URL=$(DB_URL qulsi_files "$DB_PW_FILES") LLM_BASE_URL=$LLM_BASE \
                     LLM_API_KEY=$LITELLM_MASTER_KEY FILES_DIR=${FILES_DIR:-$DATA/files} ;;
    vision)   export VISION_URL=http://127.0.0.1:8200 ;;
    web)      export SEARXNG_URL ;;
    google|microsoft|nas)
      # Rolle anlegen, falls die Datenbank älter ist als diese Connectoren (darf mehrfach laufen)
      local dbh=${QULSI_DB_HOST:-127.0.0.1:5432} pw_var="DB_PW_${name^^}"
      for _ in 1 2 3 4 5; do
        PGPASSWORD=$POSTGRES_PASSWORD PGHOST_INIT=${dbh%%:*} PGPORT=${dbh##*:} POSTGRES_USER=qulsi POSTGRES_DB=qulsi \
          bash "$APP/db/init/10_connector_konten.sh" >/dev/null 2>&1 && break
        sleep 2
      done
      export DATABASE_URL=$(DB_URL "qulsi_$name" "${!pw_var:-}") KONTO_DIR=$DATA/konten/$name \
             LLM_BASE_URL=$LLM_BASE LLM_API_KEY=$LITELLM_MASTER_KEY ;;
  esac
  # Einmalige Anmeldung bei Google/Microsoft: qulsi.sh anmelden <name>
  [ "${2:-}" = "anmelden" ] && exec "$PY" "$APP/connectors/$name/server.py" anmelden
  exec "$PY" "$APP/connectors/$name/server.py"
}

case "$1" in
  postgres)
    exec runuser -u postgres -- /usr/lib/postgresql/16/bin/postgres -D "$PGDATA_DIR" \
      -c listen_addresses=127.0.0.1 -c port=5432 ;;
  pg-sicherung)   # nur wenn Postgres auf der Container-Disk liegt (pg-lokal)
    while true; do sleep 600; bash "$APP/runpod/pg-sicherung.sh" || true; done ;;
  qdrant)
    mkdir -p "$DATA/qdrant" && cd "$DATA/qdrant"
    export QDRANT__STORAGE__STORAGE_PATH=$DATA/qdrant/storage QDRANT__STORAGE__SNAPSHOTS_PATH=$DATA/qdrant/snapshots \
           QDRANT__SERVICE__HOST=127.0.0.1 QDRANT__SERVICE__HTTP_PORT=6333 QDRANT__SERVICE__GRPC_PORT=6334 \
           QDRANT__TELEMETRY_DISABLED=true
    exec "$BIN/qdrant" ;;
  llm)   llama bonsai "$PORT_BONSAI" ;;
  klein) llama klein "$PORT_KLEIN" ;;
  embed) llama embed "$PORT_EMBED" ;;
  litellm)
    unset DATABASE_URL   # sonst versucht LiteLLM, eine eigene Datenbank (Prisma) anzulegen
    export BONSAI_URL=http://127.0.0.1:$PORT_BONSAI/v1 KLEIN_URL=http://127.0.0.1:$PORT_KLEIN/v1 EMBED_URL=http://127.0.0.1:$PORT_EMBED/v1
    exec "$LITELLM_VENV/bin/litellm" --config "$APP/config/litellm.yaml" --host 127.0.0.1 --port 4000 ;;
  voice)
    cuda_libs
    export VOICE_MODELS=$MODELS/voice HF_HOME=$MODELS/hf STT_DEVICE=${STT_DEVICE:-cuda}
    if tts_an; then   # Qwen3-TTS im eigenen Dienst (eigene Stimme), Piper springt bei Fehlern ein
      export TTS_PROVIDER=openai TTS_URL=http://127.0.0.1:$PORT_TTS/v1 TTS_MODEL=qwen3-tts TTS_ERSATZ=piper
      tts_cb_an && export TTS_URL_CHATTERBOX=http://127.0.0.1:$PORT_TTS_CB/v1   # Stimmen "chatterbox..."
    else
      export TTS_PROVIDER=piper
    fi
    exec "$PY" -m uvicorn qulsi_voice.main:app --app-dir "$APP/voice" --host 127.0.0.1 --port 8300 ;;
  tts)   # Qwen3-TTS: lädt gleich nach dem Start und bleibt im Grafikspeicher (TTS_LEERLAUF_SEKUNDEN=0)
    cuda_libs
    cd "$APP/tts"
    export TTS_MODELL=qwen3-tts TTS_MODELLE=$MODELS/tts TTS_STIMME_DIR=$APP/config/stimme HF_HOME=$MODELS/hf \
           HF_HUB_OFFLINE=1 TTS_HOST=127.0.0.1 TTS_PORT=$PORT_TTS TTS_LEERLAUF_SEKUNDEN=${TTS_LEERLAUF_SEKUNDEN:-0} \
           PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
    exec "$TTS_VENV/bin/python" qulsi_tts.py dienst ;;
  tts-chatterbox)   # Chatterbox: lädt erst, wenn die App es auswählt oder es spricht, und bleibt dann geladen
    cuda_libs
    cd "$APP/tts"
    export TTS_MODELL=chatterbox TTS_MODELLE=$MODELS/tts TTS_STIMME_DIR=$APP/config/stimme HF_HOME=$MODELS/hf \
           HF_HUB_OFFLINE=1 TTS_HOST=127.0.0.1 TTS_PORT=$PORT_TTS_CB \
           TTS_LEERLAUF_SEKUNDEN=${TTS_CHATTERBOX_LEERLAUF_SEKUNDEN:-0} TTS_VORLADEN=${TTS_CHATTERBOX_VORLADEN:-0} \
           PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
    exec "$CB_VENV/bin/python" qulsi_tts.py dienst ;;
  ntfy)
    exec "$BIN/ntfy" serve --listen-http :8090 --base-url "$NTFY_PUBLIC_URL" --behind-proxy \
      --cache-file "$DATA/ntfy/cache.db" --auth-file "$DATA/ntfy/auth.db" --auth-default-access deny-all ;;
  tresor)
    mkdir -p "$DATA/tresor" && chmod 700 "$DATA/tresor"
    export DATA_DIR=$DATA/tresor CONNECTORS_DIR=$APP/connectors HOST=127.0.0.1 PORT=$PORT_TRESOR TRESOR_LOKAL=1 \
           HTTPS_PROXY=http://tresor:x@127.0.0.1:3128 https_proxy=http://tresor:x@127.0.0.1:3128 \
           NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost
    exec "$PY" "$APP/tresor/qulsi_tresor.py" dienst ;;
  egress)
    export CONNECTORS_DIR=$APP/connectors HOST=127.0.0.1 PORT=3128
    exec "$PY" "$APP/egress/qulsi_egress.py" ;;
  connector-*) connector "${1#connector-}" "${2:-}" ;;
  vision)
    mkdir -p "$MODELS/hf" "$MODELS/ultralytics" && cd "$MODELS"   # YOLO-World lädt seine Gewichte hierher
    export DATABASE_URL=$(DB_URL qulsi_vision "$DB_PW_VISION")
    export CAMERAS_CONFIG=$APP/config/cameras.yaml HF_HOME=$MODELS/hf YOLO_CONFIG_DIR=$MODELS/ultralytics \
           DEVICE=${DEVICE:-cuda} BLE=${BLE:-false} DAUERBETRIEB=${KAMERAS_LIVE:-false} LLM_BASE_URL=$LLM_BASE LLM_API_KEY=$LITELLM_MASTER_KEY
    exec "$PY" -m uvicorn qulsi_vision.main:app --app-dir "$APP/vision" --host 127.0.0.1 --port 8200 ;;
  core)
    for c in "${!CONNECTOR_PORT[@]}"; do
      export "QULSI_CONNECTOR_${c^^}_URL=http://127.0.0.1:${CONNECTOR_PORT[$c]}/mcp"
    done
    [ -z "${SEARXNG_URL:-}" ] && export QULSI_CONNECTOR_WEB=aus
    [[ ",${QULSI_DISABLE:-}," == *",vision,"* ]] && export QULSI_CONNECTOR_VISION=aus
    [ -z "${HA_URL:-}" ] && export QULSI_CONNECTOR_HOME_ASSISTANT=aus   # läuft dann nicht (start.sh), nicht abfragen
    for c in "${!CONNECTOR_PORT[@]}"; do   # mit QULSI_DISABLE abgeschaltete Connectoren ebenso
      [[ ",${QULSI_DISABLE:-}," == *",connector-$c,"* ]] && export "QULSI_CONNECTOR_${c^^}=aus"
    done
    export DATABASE_URL LLM_BASE_URL=$LLM_BASE LLM_API_KEY=$LITELLM_MASTER_KEY \
           VOICE_URL=http://127.0.0.1:8300 EGRESS_URL=http://127.0.0.1:3128 CONNECTORS_DIR=$APP/connectors \
           CAMERAS_CONFIG=$APP/config/cameras.yaml POLICY_PATH=$APP/config/policy.yaml ROUTER_PATH=$APP/config/router.yaml PROMPT_PATH=$APP/config/agent_prompt.md \
           NTFY_URL=http://127.0.0.1:8090 FOTOS_DIR=${FOTOS_DIR:-$DATA/fotos} MEMCHIP_DIR=${MEMCHIP_DIR:-$DATA/memchip} \
           HOCHLADEN_DATEIEN_DIR=${FILES_DIR:-$DATA/files}/Hochgeladen \
           TTS_DIENST_URL=$(tts_an && echo "http://127.0.0.1:$PORT_TTS") \
           TTS_CHATTERBOX_DIENST_URL=$(tts_cb_an && echo "http://127.0.0.1:$PORT_TTS_CB") \
           QULSI_BILDER=$([ -n "${BONSAI_MMPROJ:-}" ] && [ -f "$MODELS/${BONSAI_MMPROJ:-}" ] && echo an || echo aus) \
           QULSI_LAUFZEIT=runpod STIMME_DIR=$APP/config/stimme TRESOR_URL=$TRESOR_LOKAL \
           QULSI_ENV_FILE=$ENV_FILE QULSI_KONTEN_DIR=$DATA/konten QULSI_SUPERVISOR_CONF=$DATA/supervisord.conf
    # qulsi.sh daten ...: persönliche Daten anzeigen/löschen, gleiche Umgebung wie der Core
    [ "${2:-}" = "daten" ] && cd "$APP/core" && exec "$PY" -m qulsi_core.zuruecksetzen "${@:3}"
    # qulsi.sh pin: Einrichtungscode für die Freigabe-PIN (nur auf der Box sichtbar)
    [ "${2:-}" = "pin-code" ] && cd "$APP/core" && exec "$PY" -m qulsi_core.pin_code "${@:3}"
    # qulsi.sh pruefen: Abnahme mit den echten Testsätzen gegen den laufenden Core
    [ "${2:-}" = "pruefen" ] && cd "$APP/core" && exec "$PY" -m qulsi_core.abnahme "${@:3}"
    exec "$PY" -m uvicorn qulsi_core.main:app --app-dir "$APP/core" --host 0.0.0.0 --port 8100 ;;
  open-webui)
    export OPENAI_API_BASE_URLS=http://127.0.0.1:8100/v1 OPENAI_API_KEYS=$QULSI_API_KEY ENABLE_OLLAMA_API=false \
           DATA_DIR=$DATA/open-webui ANONYMIZED_TELEMETRY=false HF_HOME=$MODELS/hf
    exec "$WEBUI_VENV/bin/open-webui" serve --host 0.0.0.0 --port 3000 ;;
  *) echo "Unbekannter Dienst: $1" >&2; exit 1 ;;
esac
