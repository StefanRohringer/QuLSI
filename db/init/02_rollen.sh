#!/bin/bash
# Eigene Datenbank-Rollen je Dienst: Jeder Dienst darf nur die Tabellen, die er braucht.
# Passwörter kommen aus der Umgebung (docker-compose bzw. runpod/start.sh).
set -euo pipefail
PSQL=(psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER:-qulsi}" --dbname "${POSTGRES_DB:-qulsi}")
[ -n "${PGHOST_INIT:-}" ] && PSQL+=(--host "$PGHOST_INIT")

"${PSQL[@]}" <<SQL
-- Vision: Objekte, Zonen, Sichtungen, Ereignisse
CREATE ROLE qulsi_vision LOGIN PASSWORD '${DB_PW_VISION}';
GRANT SELECT, INSERT, UPDATE ON objects, zones, sightings, relations TO qulsi_vision;
GRANT DELETE ON relations, sightings TO qulsi_vision;
GRANT SELECT ON system_state TO qulsi_vision;
GRANT INSERT ON ereignisse TO qulsi_vision;
GRANT EXECUTE ON FUNCTION cleanup_old_data(INT, INT) TO qulsi_vision;

-- Kalender-Connector: nur der Termin-Cache
CREATE ROLE qulsi_calendar LOGIN PASSWORD '${DB_PW_CALENDAR}';
GRANT SELECT, INSERT, UPDATE, DELETE ON termine TO qulsi_calendar;
GRANT INSERT ON ereignisse TO qulsi_calendar;

-- Mail-Connector: nur Entwürfe
CREATE ROLE qulsi_mail LOGIN PASSWORD '${DB_PW_MAIL}';
GRANT SELECT, INSERT, UPDATE ON mail_entwuerfe TO qulsi_mail;

-- Dateien-Connector: Dokumentliste
CREATE ROLE qulsi_files LOGIN PASSWORD '${DB_PW_FILES}';
GRANT SELECT, INSERT, UPDATE, DELETE ON dokumente TO qulsi_files;

DO \$\$ DECLARE r TEXT; BEGIN
  FOREACH r IN ARRAY ARRAY['qulsi_vision','qulsi_calendar','qulsi_mail','qulsi_files'] LOOP
    EXECUTE format('GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO %I', r);
  END LOOP;
END \$\$;
SQL
