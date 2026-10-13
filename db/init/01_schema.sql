-- QuLSI: strukturiertes Gedächtnis in PostgreSQL.
-- Vektoren liegen in Qdrant (Collections erinnerungen, dokumente, objekte),
-- Dateien (Bilder, PDFs, Audio) im Object Storage. Hier stehen die Fakten und Verweise.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ===========================================================================
-- Personen, Projekte, Aufgaben, Termine
-- ===========================================================================

CREATE TABLE personen (
    id         BIGSERIAL PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    aliase     TEXT[] NOT NULL DEFAULT '{}',
    beziehung  TEXT,                              -- "Kollege", "Bruder", ...
    kontakt    JSONB NOT NULL DEFAULT '{}',       -- {"mail": "...", "telefon": "..."}
    notizen    TEXT,
    erstellt   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE projekte (
    id           BIGSERIAL PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,            -- "Wohnung"
    beschreibung TEXT,
    status       TEXT NOT NULL DEFAULT 'aktiv' CHECK (status IN ('aktiv', 'pausiert', 'fertig')),
    oberprojekt  BIGINT REFERENCES projekte(id) ON DELETE SET NULL,  -- "Küche renovieren" gehört zu "Wohnung"
    erstellt     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE aufgaben (
    id         BIGSERIAL PRIMARY KEY,
    titel      TEXT NOT NULL,
    projekt_id BIGINT REFERENCES projekte(id) ON DELETE SET NULL,
    faellig    DATE,
    status     TEXT NOT NULL DEFAULT 'offen' CHECK (status IN ('offen', 'erledigt')),
    erstellt   TIMESTAMPTZ NOT NULL DEFAULT now(),
    erledigt   TIMESTAMPTZ
);

-- Kalender-Cache: der Kalender-Connector spiegelt hierher, damit Termine auch offline da sind.
CREATE TABLE termine (
    id           BIGSERIAL PRIMARY KEY,
    quelle       TEXT NOT NULL,                   -- "ics:Privat" oder "lokal"
    uid          TEXT NOT NULL,
    titel        TEXT NOT NULL,
    start        TIMESTAMPTZ NOT NULL,
    ende         TIMESTAMPTZ,
    ganztags     BOOLEAN NOT NULL DEFAULT false,
    ort          TEXT,
    beschreibung TEXT,
    aktualisiert TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (quelle, uid, start)
);
CREATE INDEX termine_start ON termine (start);

-- ===========================================================================
-- Erinnerungen und Dokumente (Text hier, Vektor in Qdrant mit gleicher id)
-- ===========================================================================

CREATE TABLE erinnerungen (
    id         BIGSERIAL PRIMARY KEY,
    art        TEXT NOT NULL CHECK (art IN ('episodisch', 'semantisch', 'praeferenz', 'projekt')),
    text       TEXT NOT NULL,
    person_id  BIGINT REFERENCES personen(id) ON DELETE SET NULL,
    projekt_id BIGINT REFERENCES projekte(id) ON DELETE SET NULL,
    zeit       TIMESTAMPTZ NOT NULL DEFAULT now(),  -- wann es passiert ist bzw. gelernt wurde
    quelle     TEXT NOT NULL DEFAULT 'gespraech',
    tsv        tsvector GENERATED ALWAYS AS (to_tsvector('german', text)) STORED
);
CREATE INDEX erinnerungen_tsv ON erinnerungen USING gin (tsv);

CREATE TABLE dokumente (
    id         BIGSERIAL PRIMARY KEY,
    quelle     TEXT NOT NULL UNIQUE,              -- "datei:/data/files/Angebot.pdf"
    titel      TEXT,
    objekt_key TEXT,                              -- Schlüssel im Object Storage
    mime       TEXT,
    groesse    BIGINT,
    geaendert  TIMESTAMPTZ,
    indiziert  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Optionaler Wissensgraph: Beziehungen zwischen Personen, Projekten, Orten, Dingen, Ereignissen.
CREATE TABLE beziehungen (
    id        BIGSERIAL PRIMARY KEY,
    von_typ   TEXT NOT NULL,                      -- person | projekt | ort | ding | ereignis
    von       TEXT NOT NULL,
    relation  TEXT NOT NULL,                      -- "arbeitet_bei", "gehoert_zu", "angebot_von"
    zu_typ    TEXT NOT NULL,
    zu        TEXT NOT NULL,
    zeit      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (von_typ, von, relation, zu_typ, zu)
);

-- ===========================================================================
-- Timeline und Event Bus
-- ===========================================================================

-- Jedes Ereignis landet hier und wird zusätzlich per NOTIFY 'qulsi_events' verteilt.
CREATE TABLE ereignisse (
    id      BIGSERIAL PRIMARY KEY,
    zeit    TIMESTAMPTZ NOT NULL DEFAULT now(),
    art     TEXT NOT NULL,                        -- gespraech | objekt_bewegt | kalender_sync | werkzeug | ...
    quelle  TEXT NOT NULL,                        -- core | vision | connector:calendar | ...
    text    TEXT NOT NULL,
    daten   JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX ereignisse_zeit ON ereignisse (zeit DESC);

CREATE FUNCTION ereignis_melden() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('qulsi_events', json_build_object(
        'id', NEW.id, 'zeit', NEW.zeit, 'art', NEW.art, 'quelle', NEW.quelle, 'text', NEW.text)::text);
    RETURN NEW;
END $$;
CREATE TRIGGER ereignis_notify AFTER INSERT ON ereignisse FOR EACH ROW EXECUTE FUNCTION ereignis_melden();

-- ===========================================================================
-- Vision: Objekte, Zonen, Sichtungen (Bild-Fingerabdrücke in Qdrant "objekte")
-- ===========================================================================

CREATE TABLE objects (
    id        BIGSERIAL PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE,
    aliases   TEXT[] NOT NULL DEFAULT '{}',
    typ       TEXT NOT NULL,                      -- Erkennungsbegriff, z. B. "keys"
    besitzer  TEXT,
    ble_tag   TEXT UNIQUE,
    erstellt  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE zones (
    id       BIGSERIAL PRIMARY KEY,
    name     TEXT NOT NULL,
    raum     TEXT NOT NULL,
    kamera   TEXT,
    polygon  JSONB,
    UNIQUE (name, kamera)
);

CREATE TABLE sightings (
    id               BIGSERIAL PRIMARY KEY,
    object_id        BIGINT REFERENCES objects(id) ON DELETE CASCADE,
    label            TEXT NOT NULL,
    zone_id          BIGINT REFERENCES zones(id) ON DELETE SET NULL,
    kamera           TEXT,
    erstmals         TIMESTAMPTZ NOT NULL DEFAULT now(),
    zuletzt          TIMESTAMPTZ NOT NULL DEFAULT now(),
    sicherheit       REAL NOT NULL,
    aehnlichkeit     REAL,
    quelle           TEXT NOT NULL CHECK (quelle IN ('kamera', 'ble', 'manuell')),
    box              JSONB,
    ausschnitt_key   TEXT                           -- Bildausschnitt im Object Storage
);
CREATE INDEX sightings_object_time ON sightings (object_id, zuletzt DESC);
CREATE INDEX sightings_label_time  ON sightings (label, zuletzt DESC);

CREATE TABLE relations (
    sighting_id  BIGINT NOT NULL REFERENCES sightings(id) ON DELETE CASCADE,
    neben        TEXT NOT NULL,
    PRIMARY KEY (sighting_id, neben)
);

-- ===========================================================================
-- Mail-Entwürfe (Bonsai > Entwurf > Freigabe > Senden)
-- ===========================================================================

CREATE TABLE mail_entwuerfe (
    id        BIGSERIAL PRIMARY KEY,
    an        TEXT NOT NULL,
    betreff   TEXT NOT NULL,
    text      TEXT NOT NULL,
    erstellt  TIMESTAMPTZ NOT NULL DEFAULT now(),
    gesendet  TIMESTAMPTZ
);

-- ===========================================================================
-- Policy Engine: Freigaben, Protokoll, Systemzustand
-- ===========================================================================

CREATE TABLE confirmations (
    id          UUID PRIMARY KEY,
    token_hash  TEXT NOT NULL,
    tool        TEXT NOT NULL,
    arguments   JSONB NOT NULL,
    begruendung TEXT,
    vorschau    TEXT,                               -- z. B. der Mail-Entwurf, wird auf dem Handy angezeigt
    kanal       TEXT NOT NULL,
    belastet    BOOLEAN NOT NULL DEFAULT false,
    status      TEXT NOT NULL DEFAULT 'offen'
                CHECK (status IN ('offen', 'freigegeben', 'abgelehnt', 'abgelaufen', 'fehler')),
    ergebnis    TEXT,
    erstellt    TIMESTAMPTZ NOT NULL DEFAULT now(),
    laeuft_ab   TIMESTAMPTZ NOT NULL,
    entschieden TIMESTAMPTZ
);

CREATE TABLE audit_log (
    id         BIGSERIAL PRIMARY KEY,
    zeit       TIMESTAMPTZ NOT NULL DEFAULT now(),
    tool       TEXT NOT NULL,
    connector  TEXT,
    stufe      TEXT NOT NULL,
    entscheid  TEXT NOT NULL,
    kanal      TEXT,
    belastet   BOOLEAN NOT NULL DEFAULT false,
    arguments  JSONB,
    ergebnis   TEXT
);
CREATE INDEX audit_time ON audit_log (zeit DESC);

CREATE TABLE system_state (
    key    TEXT PRIMARY KEY,
    value  JSONB NOT NULL,
    zeit   TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO system_state (key, value) VALUES ('privacy_mode', 'false');

CREATE FUNCTION cleanup_old_data(event_days INT DEFAULT 90, sighting_days INT DEFAULT 90)
RETURNS VOID LANGUAGE sql SECURITY DEFINER AS $$
    DELETE FROM ereignisse WHERE zeit < now() - make_interval(days => event_days) AND art IN ('objekt_bewegt', 'werkzeug');
    DELETE FROM sightings WHERE zuletzt < now() - make_interval(days => sighting_days);
    UPDATE confirmations SET status = 'abgelaufen' WHERE status = 'offen' AND laeuft_ab < now();
$$;
