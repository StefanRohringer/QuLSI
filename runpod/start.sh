#!/usr/bin/env bash
# Startet QuLSI auf einem RunPod-Pod.  bash /workspace/qulsi/runpod/start.sh
#
# Beim ersten Mal: Einstellungen mit zufälligen Schlüsseln anlegen, alles installieren,
# Bonsai-Fork bauen, Modelle laden, Datenbank anlegen. Danach startet supervisord alle Dienste.
#
# QULSI_ENABLE=open-webui     zusätzlich Open WebUI starten (Port 3000)
# QULSI_DISABLE=vision,voice  Dienste weglassen
set -euo pipefail
source "$(dirname "$0")/paths.sh"
mkdir -p "$DATA"

# Postgres braucht einen Datenordner, der dem Nutzer postgres gehört. RunPod-Netzspeicher (auch die
# "Volume Disk" in manchen Regionen, z. B. mfs#euro-2) erlaubt kein chown: dann läuft Postgres auf der
# Container-Disk und wird regelmäßig auf das Volume gesichert.
if [ -z "${QULSI_DB_HOST:-}" ]; then
  probe="$DATA/.chown-test.$$"
  touch "$probe"
  if chown 65534:65534 "$probe" 2>/dev/null && [ "$(stat -c %u "$probe")" = 65534 ]; then
    rm -f "$DATA/pg-lokal"
  else
    touch "$DATA/pg-lokal"
    cat <<EOF

HINWEIS: $DATA liegt auf Netzspeicher ohne Dateibesitzer (chown geht nicht).
Die Datenbank läuft deshalb auf der Container-Disk und wird alle 10 Minuten nach
$DATA/pg-sicherung gesichert; beim nächsten Start wird sie von dort wiederhergestellt.
Den Pod immer mit  bash $APP/runpod/qulsi.sh stop  anhalten, sonst sind Änderungen
seit der letzten Sicherung (höchstens 10 Minuten) verloren.

EOF
  fi
  rm -f "$probe"
fi
source "$(dirname "$0")/paths.sh"   # PGDATA_DIR hängt von pg-lokal ab

# ---------------------------------------------------------------- Einstellungen
if [ ! -f "$ENV_FILE" ]; then
  echo "== Einstellungen anlegen: $ENV_FILE"
  rnd() { python3 -c "import secrets; print(secrets.token_hex(20))"; }
  POD=${RUNPOD_POD_ID:-}
  if [ -n "$POD" ]; then
    PUB="https://${POD}-8100.proxy.runpod.net"; PUB_NTFY="https://${POD}-8090.proxy.runpod.net"
  else
    PUB="http://localhost:8100"; PUB_NTFY="http://localhost:8090"
  fi
  cat > "$ENV_FILE" <<EOF
# QuLSI auf RunPod. Nach Änderungen: bash $APP/runpod/start.sh
TZ=Europe/Berlin
POSTGRES_PASSWORD=$(rnd)
LITELLM_MASTER_KEY=sk-$(rnd)
QULSI_API_KEY=$(rnd)
WEBUI_SECRET_KEY=$(rnd)
DB_PW_VISION=$(rnd)
DB_PW_CALENDAR=$(rnd)
DB_PW_MAIL=$(rnd)
DB_PW_FILES=$(rnd)
DB_PW_GOOGLE=$(rnd)
DB_PW_MICROSOFT=$(rnd)
DB_PW_NAS=$(rnd)
# Über diese Adressen erreichst du QuLSI und ntfy (RunPod-HTTP-Proxy)
PUBLIC_URL=$PUB
NTFY_PUBLIC_URL=$PUB_NTFY
NTFY_TOPIC=qulsi
NTFY_TOKEN=
QULSI_OFFLINE=auto
BONSAI_FILE=Ternary-Bonsai-2-27B-PQ2_0.gguf
BONSAI_MMPROJ=
BONSAI_CTX=49152
BONSAI_PARALLEL=2
KLEIN_FILE=Qwen3-1.7B-Q8_0.gguf
KLEIN_GPU_LAYERS=99
MODEL_SMALL=
EMBED_FILE=Qwen3-Embedding-0.6B-Q8_0.gguf
STT_PROVIDER=whisper
STT_MODEL=large-v3-turbo
# Sprachausgabe: Qwen3-TTS (kann die eigene Stimme aus der App), Piper als Rückfall. Nur Piper: TTS_PROVIDER=piper
TTS_PROVIDER=openai
TTS_VOICE=de_DE-thorsten-high
DETECTOR=owlv2   # owlv2 (Apache-2.0, Standard) oder yolo-world (schneller, aber AGPL-Bibliothek ultralytics)
EMBEDDER=siglip
# Optional: dein Home Assistant zu Hause, z. B. über Tailscale
HA_URL=
HA_TOKEN=
# Optional: Kalender (geheime iCal-Adressen, mit ; getrennt)
CALENDAR_ICS_URLS=
# Optional: Mail
IMAP_HOST=
IMAP_USER=
IMAP_PASSWORD=
SMTP_HOST=
SMTP_PORT=587
MAIL_FROM=
# Optional: eigene SearXNG-Instanz für browser.search
SEARXNG_URL=
# Optional: Konten (README, Abschnitt Konten). Anmelden: bash $APP/runpod/qulsi.sh anmelden google
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
MS_CLIENT_ID=
NAS_HOST=
NAS_SHARE=
NAS_USER=
NAS_PASSWORD=
BROWSER_ALLOWED_DOMAINS=*
EOF
  chmod 600 "$ENV_FILE" 2>/dev/null || true   # auf Netzspeicher ohne Rechte wirkungslos
fi
# Ältere Einstellungen: Passwörter für die Konten-Connectoren ergänzen
for k in DB_PW_GOOGLE DB_PW_MICROSOFT DB_PW_NAS; do
  grep -q "^$k=" "$ENV_FILE" || echo "$k=$(python3 -c 'import secrets; print(secrets.token_hex(20))')" >> "$ENV_FILE"
done

# Ältere Einstellungen: Sprachausgabe einmalig auf Qwen3-TTS umstellen (eigene Stimme), Piper bleibt Rückfall.
# Wer danach wieder TTS_PROVIDER=piper einträgt, behält das.
if [ ! -f "$DATA/tts-umgestellt" ]; then
  grep -q "^TTS_PROVIDER=piper$" "$ENV_FILE" && sed -i "s/^TTS_PROVIDER=piper$/TTS_PROVIDER=openai/" "$ENV_FILE"
  touch "$DATA/tts-umgestellt"
fi

# ---------------------------------------------------------------- Installation
bash "$APP/runpod/setup.sh"
set -a; source "$ENV_FILE"; set +a

# ---------------------------------------------------------------- Datenbank
PGBIN=/usr/lib/postgresql/16/bin
if [ -z "${QULSI_DB_HOST:-}" ]; then
  id postgres >/dev/null 2>&1 || useradd -r -m postgres
  mkdir -p "$PGDATA_DIR"
  chown -R postgres:postgres "$PGDATA_DIR"
  chmod 700 "$PGDATA_DIR"
  if [ ! -f "$PGDATA_DIR/PG_VERSION" ]; then
    PWF=$(mktemp); echo "$POSTGRES_PASSWORD" > "$PWF"; chown postgres "$PWF"
    runuser -u postgres -- "$PGBIN/initdb" -D "$PGDATA_DIR" -U qulsi --pwfile="$PWF" -A scram-sha-256 -E UTF8 --locale=C.UTF-8 >/dev/null
    shred -u "$PWF" 2>/dev/null || unlink "$PWF"
    runuser -u postgres -- "$PGBIN/pg_ctl" -D "$PGDATA_DIR" -o "-c listen_addresses=127.0.0.1" -w start >/dev/null
    export PGPASSWORD=$POSTGRES_PASSWORD
    if [ -s "$PG_SICHERUNG/qulsi.dump" ]; then
      echo "== Datenbank aus der Sicherung wiederherstellen ($(date -r "$PG_SICHERUNG/qulsi.dump" '+%d.%m. %H:%M'))"
      # Rollen der Connectoren (mit ihren Passwörtern), dann die Datenbank
      [ -s "$PG_SICHERUNG/rollen.sql" ] && grep -v -E '^(CREATE|ALTER) ROLE qulsi( |;)' "$PG_SICHERUNG/rollen.sql" \
        | psql -h 127.0.0.1 -U qulsi -d postgres -q >/dev/null
      psql -h 127.0.0.1 -U qulsi -d postgres -qc "CREATE DATABASE qulsi"
      "$PGBIN/pg_restore" -h 127.0.0.1 -U qulsi -d qulsi "$PG_SICHERUNG/qulsi.dump" \
        || echo "Warnung: pg_restore meldete Fehler, Details oben" >&2
    else
      echo "== Datenbank anlegen"
      psql -h 127.0.0.1 -U qulsi -d postgres -qc "CREATE DATABASE qulsi"
      psql -h 127.0.0.1 -U qulsi -d qulsi -q -f "$APP/db/init/01_schema.sql"
      PGHOST_INIT=127.0.0.1 POSTGRES_USER=qulsi POSTGRES_DB=qulsi bash "$APP/db/init/02_rollen.sh"
      PGHOST_INIT=127.0.0.1 POSTGRES_USER=qulsi POSTGRES_DB=qulsi bash "$APP/db/init/10_connector_konten.sh"
    fi
    unset PGPASSWORD
    runuser -u postgres -- "$PGBIN/pg_ctl" -D "$PGDATA_DIR" -w stop >/dev/null
  fi
fi

# ---------------------------------------------------------------- Bildteil von Bonsai (mmproj)
# Ohne mmproj kann Bonsai keine Bilder ansehen ("Bilder" in der App ist dann nicht verfügbar).
if [ -z "${BONSAI_MMPROJ:-}" ] && [[ ",${QULSI_DISABLE:-}," != *",llm,"* ]]; then
  echo "== Bildteil (mmproj) für Bonsai suchen"
  MMP=$(MODELS_DIR=$MODELS PYTHON="$VENV/bin/python" bash "$APP/scripts/download-models.sh" mmproj 2>/dev/null | tail -1 || true)
  if [ -n "$MMP" ] && [ -f "$MODELS/$MMP" ]; then
    tmp=$(mktemp "$ENV_FILE.XXXXXX"); grep -v "^BONSAI_MMPROJ=" "$ENV_FILE" > "$tmp" || true
    echo "BONSAI_MMPROJ=$MMP" >> "$tmp"; chmod 600 "$tmp"; mv "$tmp" "$ENV_FILE"
    BONSAI_MMPROJ=$MMP; echo "   $MMP geladen, Bilder sind verfügbar."
  else
    echo "   Kein Bildteil gefunden: Fragen zu Bildern sind nicht verfügbar (BONSAI_MMPROJ in $ENV_FILE)."
  fi
fi

# ---------------------------------------------------------------- Tresor
# Geheimnisse und Konten liegen age-verschlüsselt in $DATA/tresor (der Schlüssel liegt auf demselben Volume,
# das schützt also nur Kopien der Datei, nicht den Pod). Beim ersten Mal übernimmt er, was bisher in der
# env-Datei und in den Token-Dateien der Konten stand.
if "$VENV/bin/python" -c "import pyrage" 2>/dev/null; then
  if [ ! -f "$DATA/tresor-an" ]; then
    echo "== Tresor einrichten und bisherige Geheimnisse übernehmen"
    mkdir -p "$DATA/tresor" && chmod 700 "$DATA/tresor"
    T=(env DATA_DIR="$DATA/tresor" CONNECTORS_DIR="$APP/connectors" "$VENV/bin/python" "$APP/tresor/qulsi_tresor.py")
    "${T[@]}" importieren < "$ENV_FILE" | head -1
    # übernommene Werte aus der env-Datei löschen (liegen jetzt verschlüsselt im Tresor)
    for n in $("${T[@]}" namen); do   # Client-IDs sind kein Geheimnis, die bleiben (Anmelden auf der Kommandozeile)
      [[ "$n" == *_CLIENT_ID ]] && continue
      if grep -q "^$n=" "$ENV_FILE"; then sed -i "/^$n=/d" "$ENV_FILE"; fi
    done
    for f in "$DATA"/konten/*/*.json; do
      [ -f "$f" ] && "${T[@]}" konto-importieren "$(basename "$f" .json)" < "$f" && rm -f "$f"
    done
    touch "$DATA/tresor-an"
  fi
  source "$APP/runpod/paths.sh"   # TRESOR_LOKAL setzen
else
  echo "Hinweis: pyrage fehlt (bash runpod/setup.sh einmal neu laufen lassen), Geheimnisse bleiben in $ENV_FILE"
fi

# ---------------------------------------------------------------- Welche Dienste?
DIENSTE="qdrant:10 llm:20 klein:20 embed:20 litellm:30 voice:30 ntfy:30 egress:30 vision:40"
[ -n "$TRESOR_LOKAL" ] && DIENSTE="$DIENSTE tresor:35"
[ -z "${QULSI_DB_HOST:-}" ] && DIENSTE="postgres:10 $DIENSTE"
[ -z "${QULSI_DB_HOST:-}" ] && [ -f "$DATA/pg-lokal" ] && DIENSTE="$DIENSTE pg-sicherung:60"
for c in "${!CONNECTOR_PORT[@]}"; do DIENSTE="$DIENSTE connector-$c:40"; done
tts_an && DIENSTE="$DIENSTE tts:30"   # Qwen3-TTS, nur wenn setup.sh es einrichten konnte
tts_cb_an && DIENSTE="$DIENSTE tts-chatterbox:30"   # Chatterbox zum Vergleich, ebenso
DIENSTE="$DIENSTE core:50"
[[ ",${QULSI_ENABLE:-}," == *",open-webui,"* ]] && DIENSTE="$DIENSTE open-webui:60"
AUS=${QULSI_DISABLE:-}
[ -z "${SEARXNG_URL:-}" ] && AUS="$AUS,connector-web"           # Websuche nur mit eigener SearXNG-Instanz
[ -z "${HA_URL:-}" ] && AUS="$AUS,connector-home_assistant"      # Home Assistant nur mit HA_URL
[ "${MODEL_SMALL:-}" = "aus" ] && AUS="$AUS,klein"

# ---------------------------------------------------------------- supervisord
CONF=$DATA/supervisord.conf
mkdir -p "$DATA/logs"
{
  cat <<EOF
[supervisord]
logfile=$DATA/logs/supervisord.log
pidfile=$DATA/supervisord.pid
childlogdir=$DATA/logs

[unix_http_server]
file=/tmp/qulsi-supervisor.sock

[rpcinterface:supervisor]
supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface

[supervisorctl]
serverurl=unix:///tmp/qulsi-supervisor.sock
EOF
  for d in $DIENSTE; do
    name=${d%%:*}; prio=${d#*:}
    [[ ",$AUS," == *",$name,"* ]] && continue
    cat <<EOF

[program:$name]
command=bash $APP/runpod/run.sh $name
priority=$prio
autorestart=true
startretries=20
startsecs=3
stopasgroup=true
killasgroup=true
redirect_stderr=true
stdout_logfile=$DATA/logs/$name.log
stdout_logfile_maxbytes=10MB
stdout_logfile_backups=2
EOF
  done
} > "$CONF"

CTL=("$VENV/bin/python" -m supervisor.supervisorctl -c "$CONF")
LAEUFT=0; [ -S "/tmp/qulsi-supervisor.sock" ] && "${CTL[@]}" pid >/dev/null 2>&1 && LAEUFT=1

# Alte QuLSI-Dienste ohne Steuerung (supervisord weg, Dienste laufen noch): anbieten, sie zu beenden.
if [ $LAEUFT = 0 ] && [ -n "$(qulsi_reste)" ]; then
  echo "== Alte QuLSI-Dienste laufen noch ohne Steuerung:"
  ps -o pid=,comm= -p "$(qulsi_reste | paste -sd,)" | awk '{printf "   %s (%s)\n", $2, $1}' | sort -u -k1,1
  antwort=${QULSI_ALTE_BEENDEN:-}
  if [ -z "$antwort" ] && [ -t 0 ]; then read -r -p "   Jetzt beenden und neu starten? [J/n] " antwort; antwort=${antwort:-j}; fi
  if [[ "${antwort,,}" =~ ^(j|ja|y|yes)$ ]]; then
    qulsi_reste_beenden
  else
    echo "   Abgebrochen. Beenden mit: bash $APP/runpod/qulsi.sh stop  (oder QULSI_ALTE_BEENDEN=ja bash $APP/runpod/start.sh)" >&2
    exit 1
  fi
fi

# Vor dem ersten Start: sind die Ports frei? (Auf RunPod belegt z. B. dessen nginx eigene Ports.)
if [ $LAEUFT = 0 ]; then
  declare -A PORTS=([qdrant]=6333 [llm]=$PORT_BONSAI [klein]=$PORT_KLEIN [embed]=$PORT_EMBED [litellm]=4000
                    [voice]=8300 [vision]=8200 [ntfy]=8090 [egress]=3128 [core]=8100 [open-webui]=3000
                    [tresor]=$PORT_TRESOR [tts]=$PORT_TTS [tts-chatterbox]=$PORT_TTS_CB)
  [ -z "${QULSI_DB_HOST:-}" ] && PORTS[postgres]=5432
  for c in "${!CONNECTOR_PORT[@]}"; do PORTS[connector-$c]=${CONNECTOR_PORT[$c]}; done
  BELEGT=""
  for d in $DIENSTE; do
    name=${d%%:*}; port=${PORTS[$name]:-}
    [ -z "$port" ] || [[ ",$AUS," == *",$name,"* ]] && continue
    if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
      wer=$( (ss -ltnp 2>/dev/null || true) | awk -v p=":$port" '$4 ~ p"$" {print $6}' | head -1)
      BELEGT="$BELEGT\n  Port $port für $name ist schon belegt ${wer:+($wer)}"
    fi
  done
  if [ -n "$BELEGT" ]; then
    echo -e "\nFEHLER: Ports sind belegt:$BELEGT" >&2
    echo -e "Entweder das andere Programm beenden oder QuLSI einen anderen Port geben, z. B.\n  eine Zeile  QULSI_PORT_EMBED=8084  in $ENV_FILE  (auch QULSI_PORT_BONSAI, QULSI_PORT_KLEIN,\n  QULSI_PORT_CONNECTOREN=19000 für alle Connectoren)\n" >&2
    exit 1
  fi
fi

if [ $LAEUFT = 1 ]; then
  echo "== Dienste neu laden"
  "${CTL[@]}" reread >/dev/null
  "${CTL[@]}" update
  "${CTL[@]}" restart all
else
  echo "== Dienste starten"
  "$VENV/bin/python" -m supervisor.supervisord -c "$CONF"
fi

# ---------------------------------------------------------------- ntfy: eigenes Thema
# Freigaben laufen über die App mit der Freigabe-PIN, ntfy ist vorerst aus. Ein erratbares Thema
# "qulsi" wird trotzdem durch ein zufälliges ersetzt, falls ntfy später eingeschaltet wird.
if [ "${NTFY_TOPIC:-qulsi}" = qulsi ]; then
  tmp=$(mktemp "$ENV_FILE.XXXXXX")
  grep -v "^NTFY_TOPIC=" "$ENV_FILE" > "$tmp" || true
  printf "NTFY_TOPIC=qulsi-%s\n" "$(python3 -c 'import secrets; print(secrets.token_hex(8))')" >> "$tmp"
  chmod 600 "$tmp"; mv "$tmp" "$ENV_FILE"
  set -a; source "$ENV_FILE"; set +a
  "${CTL[@]}" restart core >/dev/null 2>&1 || true
fi

[ -f "$APP/VERSION" ] && { echo; cat "$APP/VERSION"; }
cat <<EOF

QuLSI startet. Bonsai braucht beim ersten Laden etwa eine Minute.
  Status:     bash $APP/runpod/qulsi.sh status
  Logs:       bash $APP/runpod/qulsi.sh logs core      (oder llm, vision, egress, connector-calendar, ...)
  Test:       bash $APP/runpod/qulsi.sh test

  QuLSI-App:  $PUBLIC_URL     Schlüssel: ${QULSI_API_KEY:0:4}…  (ganz: bash $APP/runpod/qulsi.sh schluessel, nicht in Chats kopieren)
  API:        $PUBLIC_URL/v1  (OpenAI-kompatibel, Modell "qulsi")
  Freigaben:  in der App unter Heute, mit der Freigabe-PIN
  Abnahme:    bash $APP/runpod/qulsi.sh pruefen   (vor dem Weitergeben, muss grün sein)
EOF
# Noch keine Freigabe-PIN: Einrichtungscode zeigen, damit sie in der App festgelegt werden kann
for _ in $(seq 30); do bash "$APP/runpod/run.sh" core pin-code --wenn-leer 2>/dev/null && break; sleep 2; done
