"""Tabellen des Gedächtnisses, die nach 01_schema.sql dazukamen.

Der Core spielt das beim Start ein (idempotent). So bekommen auch bestehende
Installationen die neuen Spalten, ohne die Datenbank neu anzulegen.
"""

from __future__ import annotations

from .store import Store

MIGRATION = """
-- Orte: aus Terminen, Fotos (GPS) oder vom Nutzer ("Zuhause", "Büro")
CREATE TABLE IF NOT EXISTS orte (
    id        BIGSERIAL PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE,
    aliase    TEXT[] NOT NULL DEFAULT '{}',
    art       TEXT,                                    -- zuhause | arbeit | stadt | lokal | ...
    lat       DOUBLE PRECISION,
    lon       DOUBLE PRECISION,
    radius_m  INT NOT NULL DEFAULT 300,
    erstellt  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE erinnerungen ADD COLUMN IF NOT EXISTS ort_id BIGINT REFERENCES orte(id) ON DELETE SET NULL;
ALTER TABLE erinnerungen ADD COLUMN IF NOT EXISTS quelle_ref TEXT;     -- z. B. termin:Privat:uid:start, verhindert Doppelte
ALTER TABLE erinnerungen ADD COLUMN IF NOT EXISTS objekt_key TEXT;     -- Foto oder Datei im Object Storage
ALTER TABLE erinnerungen ADD COLUMN IF NOT EXISTS daten JSONB NOT NULL DEFAULT '{}';
ALTER TABLE erinnerungen ADD COLUMN IF NOT EXISTS fremd BOOLEAN NOT NULL DEFAULT false;  -- Text stammt aus Mail, Datei, Kalender
ALTER TABLE erinnerungen ADD COLUMN IF NOT EXISTS eingebettet BOOLEAN NOT NULL DEFAULT false;
CREATE UNIQUE INDEX IF NOT EXISTS erinnerungen_quelle_ref ON erinnerungen (quelle_ref);
CREATE INDEX IF NOT EXISTS erinnerungen_zeit ON erinnerungen (zeit DESC);
CREATE INDEX IF NOT EXISTS erinnerungen_ort ON erinnerungen (ort_id);

-- Eine Erinnerung kann mehrere Personen betreffen (person_id bleibt die Hauptperson)
CREATE TABLE IF NOT EXISTS erinnerung_personen (
    erinnerung_id BIGINT NOT NULL REFERENCES erinnerungen(id) ON DELETE CASCADE,
    person_id     BIGINT NOT NULL REFERENCES personen(id) ON DELETE CASCADE,
    PRIMARY KEY (erinnerung_id, person_id)
);
CREATE INDEX IF NOT EXISTS erinnerung_personen_person ON erinnerung_personen (person_id);

-- Fotos aus dem Foto-Ordner (Aufnahmezeit und GPS aus EXIF, Original im Object Storage)
CREATE TABLE IF NOT EXISTS fotos (
    id            BIGSERIAL PRIMARY KEY,
    quelle        TEXT NOT NULL UNIQUE,                -- foto:Urlaub/IMG_0001.jpg
    objekt_key    TEXT NOT NULL,
    aufgenommen   TIMESTAMPTZ,
    lat           DOUBLE PRECISION,
    lon           DOUBLE PRECISION,
    ort_id        BIGINT REFERENCES orte(id) ON DELETE SET NULL,
    kamera        TEXT,
    breite        INT,
    hoehe         INT,
    erinnerung_id BIGINT REFERENCES erinnerungen(id) ON DELETE SET NULL,
    erstellt      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fotos_zeit ON fotos (aufgenommen DESC);

-- Termine: extern = Titel/Beschreibung stammen von Dritten (Einladung mit fremdem Organisator).
-- Selbst angelegte (lokal, App, Chat) und eigene synchronisierte Termine sind eigene Daten.
ALTER TABLE termine ADD COLUMN IF NOT EXISTS extern BOOLEAN NOT NULL DEFAULT false;

-- Eigene Termine sind eigene Daten, kein fremder Inhalt (frühere Versionen markierten sie als fremd)
-- (bis extern bekannt war, waren alle Termin-Erinnerungen fälschlich fremd; einmalig zurücksetzen)
UPDATE erinnerungen SET fremd = false WHERE quelle = 'kalender' AND fremd AND NOT (daten ? 'extern');

-- Gespräche: was der Nutzer gefragt und QuLSI geantwortet hat (für "worüber haben wir gesprochen?").
-- Bleibt nur auf der Box; "qulsi.sh daten leeren" löscht es mit.
CREATE TABLE IF NOT EXISTS gespraeche (
    id       BIGSERIAL PRIMARY KEY,
    zeit     TIMESTAMPTZ NOT NULL DEFAULT now(),
    kanal    TEXT,
    frage    TEXT NOT NULL,
    antwort  TEXT NOT NULL,
    belastet BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS gespraeche_zeit ON gespraeche (zeit DESC);
-- Aus der App hochgeladene Fotos und Dateien (hochladen.py). Der Name ist nur Anzeige, die Datei liegt
-- unter der eigenen ID in FOTOS_DIR/Hochgeladen bzw. FILES_DIR/Hochgeladen (pfad).
CREATE TABLE IF NOT EXISTS hochgeladen (
    id            TEXT PRIMARY KEY,
    zeit          TIMESTAMPTZ NOT NULL DEFAULT now(),
    name          TEXT NOT NULL,
    art           TEXT NOT NULL,
    mime          TEXT NOT NULL,
    groesse       BIGINT NOT NULL,
    pfad          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'eingeordnet',
    hinweis       TEXT,
    vorschau_key  TEXT,
    erinnerung_id BIGINT REFERENCES erinnerungen(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS hochgeladen_zeit ON hochgeladen (zeit DESC);
"""


async def migrieren(store: Store) -> None:
    async with await store.conn() as c:
        await c.execute("SELECT pg_advisory_lock(4711)")
        try:
            await c.execute(MIGRATION)
        finally:
            await c.execute("SELECT pg_advisory_unlock(4711)")
