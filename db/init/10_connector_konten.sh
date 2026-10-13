#!/bin/bash
# Datenbank-Rollen für die Connectoren Google, Microsoft und NAS.
# Läuft beim ersten Start automatisch. Auf einer schon eingerichteten Box einmal von Hand:
#   docker compose exec postgres bash /docker-entrypoint-initdb.d/10_connector_konten.sh
# Darf mehrfach laufen (legt nur an, was fehlt, und setzt die Passwörter aus der .env).
set -euo pipefail
PSQL=(psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER:-qulsi}" --dbname "${POSTGRES_DB:-qulsi}")
[ -n "${PGHOST_INIT:-}" ] && PSQL+=(--host "$PGHOST_INIT")

rolle() {  # rolle <name> <passwort>
  [ -z "$2" ] && { echo "Kein Passwort für $1 (DB_PW_... in der .env), übersprungen"; return; }
  "${PSQL[@]}" -q -v rolle="$1" -v pw="$2" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN', :'rolle') WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'rolle') \gexec
SELECT format('ALTER ROLE %I PASSWORD %L', :'rolle', :'pw') \gexec
SQL
}

rolle qulsi_google "${DB_PW_GOOGLE:-}"
rolle qulsi_microsoft "${DB_PW_MICROSOFT:-}"
rolle qulsi_nas "${DB_PW_NAS:-}"

"${PSQL[@]}" -q <<'SQL'
DO $$ DECLARE r TEXT; BEGIN
  -- Google und Microsoft: Termine in den gemeinsamen Termin-Cache, Ereignisse in die Timeline
  FOREACH r IN ARRAY ARRAY['qulsi_google', 'qulsi_microsoft'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON termine TO %I', r);
      EXECUTE format('GRANT INSERT ON ereignisse TO %I', r);
      EXECUTE format('GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO %I', r);
    END IF;
  END LOOP;
  -- NAS: Dokumentliste fürs Gedächtnis (wie der Dateien-Connector)
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'qulsi_nas') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON dokumente TO qulsi_nas;
    GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO qulsi_nas;
  END IF;
END $$;
SQL
