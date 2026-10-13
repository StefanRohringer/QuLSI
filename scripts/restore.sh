#!/usr/bin/env bash
# Stellt die Datenbank aus dem letzten restic-Backup wieder her.
#   scripts/restore.sh            letzter Stand
#   scripts/restore.sh <snapshot> bestimmter Stand (restic snapshots zeigt alle)
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/restic.env
SNAP=${1:-latest}
TMP=$(mktemp -d)
restic restore "$SNAP" --target "$TMP" --include "$(pwd)/data/backup/qulsi.dump"
DUMP=$(find "$TMP" -name qulsi.dump | head -1)
docker compose up -d postgres
docker compose exec -T postgres pg_restore -U qulsi -d qulsi --clean --if-exists < "$DUMP"
rm -rf "$TMP"
echo "Datenbank aus $SNAP wiederhergestellt."
echo "Qdrant und Object Storage: restic restore $SNAP --target / --include \"$(pwd)/data/qdrant\" --include \"$(pwd)/data/objects\""
