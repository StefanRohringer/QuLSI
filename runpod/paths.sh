# Gemeinsame Pfade für die RunPod-Skripte. Alles Dauerhafte liegt auf dem Volume /workspace.
WS=${WORKSPACE:-/workspace}
APP=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)   # dieses Projekt
DATA=$WS/qulsi-data          # Postgres, Qdrant, Object Storage, ntfy, Einstellungen, Logs
MODELS=$WS/models            # GGUF-Dateien, Bild- und Sprachmodelle
BIN=$WS/qulsi-bin            # llama-server, qdrant, ntfy
# Python-Umgebungen auf der Container-Disk: auf dem Netzspeicher von RunPod ist das Installieren
# vieler kleiner Dateien extrem langsam. Nach der Installation liegt je Umgebung ein Archiv (eine
# große Datei) auf dem Volume; nach einem Neustart des Pods wird es in Sekunden wieder ausgepackt.
VENV_ROOT=${QULSI_VENVS:-/opt/qulsi-venvs}
VENV_ARCHIV=$WS/qulsi-venvs
VENV=$VENV_ROOT/qulsi         # Core, Connectoren, Egress, Vision, Voice
LITELLM_VENV=$VENV_ROOT/litellm
WEBUI_VENV=$VENV_ROOT/open-webui
TTS_VENV=$VENV_ROOT/tts          # Qwen3-TTS (eigene Stimme), eigene feste transformers-Version
CB_VENV=$VENV_ROOT/tts-chatterbox   # Chatterbox (natürlicheres Deutsch), braucht wieder andere Versionen
ENV_FILE=$DATA/env
# Datenbank: normal auf dem Volume. Erlaubt das Volume kein chown (RunPod-Netzspeicher), legt start.sh
# die Markierung pg-lokal an: dann liegt Postgres auf der Container-Disk und wird alle 10 Minuten
# nach $PG_SICHERUNG gesichert und beim nächsten Start von dort wiederhergestellt.
PG_SICHERUNG=$DATA/pg-sicherung
if [ -f "$DATA/pg-lokal" ]; then PGDATA_DIR=/var/lib/qulsi-pg; else PGDATA_DIR=$DATA/postgres; fi

# Ports der Modelle (nur 127.0.0.1). 8081 ist auf RunPod belegt (nginx des Pods), daher embed auf 8083.
# Andere Ports: QULSI_PORT_EMBED=8084 usw. beim Start oder als Zeile in $ENV_FILE.
if [ -f "$ENV_FILE" ]; then
  while IFS='=' read -r k v; do [ -z "${!k:-}" ] && export "$k=$v"; done \
    < <(grep -E '^QULSI_PORT_[A-Z_]+=[0-9]+$' "$ENV_FILE" || true)
fi
PORT_BONSAI=${QULSI_PORT_BONSAI:-8080}
PORT_KLEIN=${QULSI_PORT_KLEIN:-8082}
PORT_EMBED=${QULSI_PORT_EMBED:-8083}

# Connectoren und ihre Ports (nur 127.0.0.1). Nicht 8001 ff.: dort lauscht auf RunPod dessen nginx.
# Anderer Bereich: QULSI_PORT_CONNECTOREN=19000 (dann 19001 bis 19010).
C0=${QULSI_PORT_CONNECTOREN:-18000}
declare -A CONNECTOR_PORT=([home_assistant]=$((C0+1)) [calendar]=$((C0+2)) [mail]=$((C0+3)) [files]=$((C0+4))
                            [web]=$((C0+5)) [vision]=$((C0+6)) [google]=$((C0+7)) [microsoft]=$((C0+8))
                            [nas]=$((C0+9)) [browser]=$((C0+10)))

# Tresor (age-verschlüsselt) auch auf RunPod, sobald start.sh ihn eingerichtet hat (Marker tresor-an)
PORT_TRESOR=${QULSI_PORT_TRESOR:-18400}
# Sprachausgabe mit Qwen3-TTS (eigene Stimme), nur 127.0.0.1; Piper im Voice-Dienst bleibt Rückfall
PORT_TTS=${QULSI_PORT_TTS:-18401}
# TTS-Dienst nur, wenn er eingerichtet ist (setup.sh) und nicht mit QULSI_DISABLE=tts abgeschaltet
tts_an() { [ -f "$TTS_VENV/.tts-ok" ] && [[ ",${QULSI_DISABLE:-}," != *",tts,"* ]] && [ "${TTS_PROVIDER:-openai}" != piper ]; }
# Chatterbox als zweiter Sprachdienst zum Vergleich, in der App unter Einstellungen > Stimme wählbar
PORT_TTS_CB=${QULSI_PORT_TTS_CB:-18402}
tts_cb_an() { [ -f "$CB_VENV/.tts-ok" ] && [[ ",${QULSI_DISABLE:-}," != *",tts-chatterbox,"* ]] && tts_an; }
TRESOR_LOKAL=""; [ -f "$DATA/tresor-an" ] && TRESOR_LOKAL=http://127.0.0.1:$PORT_TRESOR

# Eigene QuLSI-Prozesse finden, auch wenn supervisord schon weg ist (z. B. nach einem Abbruch).
# Erkannt an Pfaden in der Befehlszeile; die Steuer-Skripte und der eigene Aufruf zählen nicht.
qulsi_reste() {
  local ich=$$ ahnen=" " p cmd
  while [ -n "$ich" ] && [ "$ich" != 0 ]; do
    ahnen+="$ich "; ich=$(awk '{print $4}' "/proc/$ich/stat" 2>/dev/null || true)
  done
  for p in /proc/[0-9]*; do
    p=${p#/proc/}
    [[ "$ahnen" == *" $p "* ]] && continue
    cmd=$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null) || continue
    [ -n "$cmd" ] || continue
    case "$cmd" in
      *runpod/qulsi.sh*|*runpod/start.sh*|*runpod/setup.sh*|*runpod/pg-sicherung.sh*) continue ;;
    esac
    case "$cmd" in
      *"$APP/"*|*"$BIN/"*|*"$VENV_ROOT/"*|*"$PGDATA_DIR"*|*"$MODELS/"*|*"$DATA/supervisord.conf"*) echo "$p" ;;
    esac
  done
}

# Beendet die Reste: erst freundlich (TERM), nach 20 Sekunden hart (KILL).
qulsi_reste_beenden() {
  local pids p
  pids=$(qulsi_reste)
  [ -n "$pids" ] || return 0
  echo "   beende $(echo "$pids" | wc -l) übrig gebliebene QuLSI-Prozesse"
  kill -TERM $pids 2>/dev/null || true
  for _ in $(seq 20); do
    pids=$(for p in $pids; do [ -n "$(tr -d '\0' < "/proc/$p/cmdline" 2>/dev/null)" ] && echo "$p"; done)  # Zombies zählen nicht
    [ -n "$pids" ] || return 0
    sleep 1
  done
  echo "   reagieren nicht, werden hart beendet: $pids" >&2
  kill -KILL $pids 2>/dev/null || true
  sleep 1
}
