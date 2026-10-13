#!/usr/bin/env bash
# Sichert die Datenbank auf das Volume ($DATA/pg-sicherung): Rollen und Datenbank, erst in .tmp,
# dann umbenannt, damit eine abgebrochene Sicherung nie die letzte gute überschreibt.
# Läuft alle 10 Minuten (Dienst pg-sicherung) und bei  qulsi.sh stop.
set -euo pipefail
source "$(dirname "$0")/paths.sh"
set -a; source "$ENV_FILE"; set +a
PGBIN=/usr/lib/postgresql/16/bin
export PGPASSWORD=$POSTGRES_PASSWORD
mkdir -p "$PG_SICHERUNG"
"$PGBIN/pg_dumpall" -h 127.0.0.1 -U qulsi --roles-only > "$PG_SICHERUNG/rollen.sql.tmp"
"$PGBIN/pg_dump" -h 127.0.0.1 -U qulsi -d qulsi -Fc -f "$PG_SICHERUNG/qulsi.dump.tmp"
mv "$PG_SICHERUNG/rollen.sql.tmp" "$PG_SICHERUNG/rollen.sql"
mv "$PG_SICHERUNG/qulsi.dump.tmp" "$PG_SICHERUNG/qulsi.dump"
echo "$(date '+%F %T') Datenbank gesichert: $(du -h "$PG_SICHERUNG/qulsi.dump" | cut -f1)"
