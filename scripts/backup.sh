#!/usr/bin/env bash
# Verschlüsseltes Backup mit restic: Datenbank-Dump, Qdrant, Object Storage, Konfiguration.
# Modelle werden nicht gesichert (lassen sich neu laden). Der Tresor wird verschlüsselt gesichert,
# sein Schlüssel (data/tresor/schluessel.txt) nicht: den getrennt aufheben, z. B. im Passwort-Manager.
#
# Einrichtung einmalig:
#   cp scripts/restic.env.example scripts/restic.env   # Ziel und Passwort eintragen
#   source scripts/restic.env && restic init
# Danach täglich per systemd-Timer (scripts/systemd/).
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/restic.env

mkdir -p data/backup
echo "== Datenbank sichern"
docker compose exec -T postgres pg_dump -U qulsi -d qulsi --format=custom > data/backup/qulsi.dump.tmp
mv data/backup/qulsi.dump.tmp data/backup/qulsi.dump

echo "== restic backup"
restic backup \
  --tag qulsi \
  --exclude data/postgres \
  --exclude models \
  --exclude data/tresor/schluessel.txt \
  --exclude data/test-signatur.key \
  .env config db docker-compose*.yml core connectors egress tresor vision voice tts runtime scripts data

echo "== Alte Stände aufräumen (7 täglich, 4 wöchentlich, 6 monatlich)"
restic forget --tag qulsi --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune
restic check --read-data-subset=5%
