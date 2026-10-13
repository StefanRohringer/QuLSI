"""Gedächtnis mit echter Datenbank (ohne Docker): Organisator, Suche nach Person/Zeit/Ort, Memchip, Policy.

Startet ein temporäres PostgreSQL (initdb/pg_ctl nötig, sonst übersprungen) oder nutzt QULSI_TEST_DSN.
Qdrant läuft im Speicher, Embeddings sind Wort-Hashes wie im fake-llm.
"""

from __future__ import annotations

import asyncio
import glob
import io
import math
import os
import re
import shutil
import socket
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[2]
TZ = ZoneInfo("Europe/Berlin")

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("qdrant_client")
pytest.importorskip("PIL")
pytest.importorskip("cryptography")

from qdrant_client import AsyncQdrantClient  # noqa: E402

from qulsi_core.memory import Graph, ObjectStore, Store, Vektoren  # noqa: E402
from qulsi_core.memory.gedaechtnis import Gedaechtnis  # noqa: E402
from qulsi_core.memory.memchip import Memchip  # noqa: E402
from qulsi_core.memory.organisator import Organisator  # noqa: E402
from qulsi_core.memory.schema import migrieren  # noqa: E402


def _pg_bin() -> Path | None:
    if shutil.which("pg_ctl"):
        return Path(shutil.which("pg_ctl")).parent
    treffer = sorted(glob.glob("/usr/lib/postgresql/*/bin/pg_ctl"))
    return Path(treffer[-1]).parent if treffer else None


@pytest.fixture(scope="module")
def dsn():
    if os.environ.get("QULSI_TEST_DSN"):
        yield os.environ["QULSI_TEST_DSN"]
        return
    bin_ = _pg_bin()
    if bin_ is None:
        pytest.skip("PostgreSQL nicht installiert")
    d = Path(tempfile.mkdtemp(prefix="qulsi-pg-"))
    os.chmod(d, 0o777)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    als = ["runuser", "-u", "nobody", "--"] if os.geteuid() == 0 else []
    run = lambda *a: subprocess.run([*als, *a], check=True, capture_output=True, text=True)  # noqa: E731
    try:
        run(str(bin_ / "initdb"), "-D", str(d / "data"), "-U", "qulsi", "-A", "trust")
        run(str(bin_ / "pg_ctl"), "-D", str(d / "data"), "-l", str(d / "log"), "-w", "start",
            "-o", f"-p {port} -k {d} -c listen_addresses=127.0.0.1")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        pytest.skip(f"PostgreSQL startet nicht: {exc}")
    url = f"postgresql://qulsi@127.0.0.1:{port}/postgres"
    with psycopg.connect(url, autocommit=True) as c:
        c.execute((ROOT / "db/init/01_schema.sql").read_text())
    yield url
    run(str(bin_ / "pg_ctl"), "-D", str(d / "data"), "-m", "immediate", "stop")
    shutil.rmtree(d, ignore_errors=True)


def sql(dsn: str, q: str, params=None) -> list[tuple]:
    with psycopg.connect(dsn, autocommit=True) as c:
        cur = c.execute(q, params)
        return cur.fetchall() if cur.description else []


class WortVektoren(Vektoren):
    """Jedes Wort bekommt eine eigene Dimension (keine Hash-Kollisionen: sonst hängt die Rangfolge vom Datum
    in den Texten ab, z. B. kollidierte "04" mit "angebot")."""

    WORTE: dict[str, int] = {}

    async def embed(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for w in re.findall(r"[\wäöüß]+", text.lower()):
            v[self.WORTE.setdefault(w, len(self.WORTE)) % self.dim] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]


class FakeHub:
    def __init__(self, mails):
        self.mails = mails
        self.aufrufe = []

    def finden(self, name):
        return name == "email.search"

    async def daten(self, name, args, kanal="app"):
        self.aufrufe.append((name, args, kanal))
        return self.mails


def foto(lat: float, lon: float, wann: str) -> bytes:
    from PIL import Image

    im = Image.new("RGB", (80, 60), "blue")
    ex = im.getexif()
    g, m = int(lat), (lat - int(lat)) * 60
    ex.get_ifd(0x8825).update({1: "N", 2: (float(g), float(int(m)), (m - int(m)) * 60), 3: "E",
                               4: (float(int(lon)), float(int((lon - int(lon)) * 60)), ((lon - int(lon)) * 60 % 1) * 60)})
    ex.get_ifd(0x8769)[36867] = wann
    b = io.BytesIO()
    im.save(b, "JPEG", exif=ex)
    return b.getvalue()


@pytest.fixture
def welt(dsn, tmp_path):
    """Frische Daten: Personen, Orte, Termine, Mails, Fotos, Datei."""
    sql(dsn, """TRUNCATE aufgaben, termine, personen, projekte, ereignisse, audit_log, erinnerungen, dokumente, beziehungen
                RESTART IDENTITY CASCADE""")
    asyncio.run(migrieren(Store(dsn)))
    asyncio.run(migrieren(Store(dsn)))  # idempotent
    sql(dsn, "TRUNCATE orte, fotos, erinnerung_personen RESTART IDENTITY CASCADE")
    jetzt = datetime.now(TZ)
    letzte_woche = (jetzt - timedelta(days=jetzt.weekday() + 7)).replace(hour=10, minute=0, second=0, microsecond=0)
    sql(dsn, "INSERT INTO personen (name, aliase, kontakt) VALUES ('Max Muster', '{Maxi}', '{\"mail\": \"max@example.org\"}'), ('Anna', '{}', '{}')")
    sql(dsn, "INSERT INTO projekte (name) VALUES ('Küche')")
    sql(dsn, """INSERT INTO termine (quelle, uid, titel, start, ort, beschreibung) VALUES
                ('ics:Privat', 'k1', 'Kaffee mit Max', %s, 'Café Luitpold, Brienner Str. 11, München', 'Über die Küche reden'),
                ('ics:Privat', 'z1', 'Zukunft', %s, NULL, NULL)""",
        (letzte_woche, jetzt + timedelta(days=3)))
    for w in range(1, 5):  # Training jeden Dienstag 19 Uhr
        di = (jetzt - timedelta(days=jetzt.weekday() - 1 + 7 * w)).replace(hour=19, minute=0, second=0, microsecond=0)
        sql(dsn, "INSERT INTO termine (quelle, uid, titel, start) VALUES ('ics:Privat', 'tr', 'Training', %s)", (di,))
    sql(dsn, """INSERT INTO dokumente (quelle, titel, objekt_key, mime, groesse, geaendert)
                VALUES ('datei:Wohnung/Angebot_Kueche.md', 'Angebot_Kueche.md', NULL, 'md', 10, %s)""", (jetzt - timedelta(days=2),))
    fotos_dir = tmp_path / "fotos"
    (fotos_dir / "2026 Ausflug").mkdir(parents=True)
    tag = (jetzt - timedelta(days=4)).strftime("%Y:%m:%d")
    for i in range(5):
        (fotos_dir / "2026 Ausflug" / f"IMG_{i}.jpg").write_bytes(foto(48.1417, 11.575, f"{tag} 14:3{i}:00"))
    (fotos_dir / "kaputt.jpg").write_bytes(b"kein Bild")
    datum = (jetzt - timedelta(days=1)).strftime("%d.%m.%Y %H:%M")
    mails = [{"id": str(i), "von": "Lena Berg <lena@example.org>", "betreff": f"Fliesen {i}", "datum": datum, "auszug": "Hallo"}
             for i in range(3)]
    mails.append({"id": "9", "von": "Max <max@example.org>", "betreff": "Termin Küche", "datum": datum, "auszug": ""})
    mails.append({"id": "10", "von": "noreply@shop.de", "betreff": "Ihre Bestellung", "datum": datum, "auszug": ""})
    store = Store(dsn)
    vek = WortVektoren("", "", "", dim=4096, client=AsyncQdrantClient(location=":memory:"))
    asyncio.run(vek.vorbereiten())
    g = Gedaechtnis(store, vek, Graph(store), "Europe/Berlin")
    objekte = ObjectStore(root=str(tmp_path / "objekte"))
    chip = Memchip(g, objekte, verzeichnis=str(tmp_path / "chip"), passwort="geheim")
    org = Organisator(g, FakeHub(mails), objekte, fotos_dir=str(fotos_dir), minuten=0, memchip=chip)
    return g, org, chip, tmp_path


def test_organisator_und_suche(welt, dsn):
    g, org, chip, _ = welt

    async def lauf():
        # München mit Koordinaten bekannt machen, bevor Fotos kommen
        await g.orte_speichern("München", 48.137, 11.575, "stadt", radius_m=5000)
        erg = await org.lauf()
        assert erg["termine"] == 5 and erg["fotos"] == 5 and erg["mails"] == 5 and erg["dateien"] == 1
        assert erg["fototage"] == 1 and erg["kontakte"] == 1 and erg["gewohnheiten"] == 1
        assert erg["rueckblick"] >= 2
        assert (await org.lauf())["termine"] == 0  # idempotent

        # Termin: Ort aus der Adresse, Max über den Vornamen erkannt, Projekt Küche verknüpft
        t = await g.store.eine("""SELECT e.*, o.name AS ort, pr.name AS projekt FROM erinnerungen e
                                  LEFT JOIN orte o ON o.id = e.ort_id LEFT JOIN projekte pr ON pr.id = e.projekt_id
                                  WHERE e.quelle_ref LIKE 'termin:ics:Privat:k1:%'""")
        assert t["ort"] == "Café Luitpold" and t["projekt"] == "Küche" and not t["fremd"]  # eigener Kalender
        assert await g.store.eine("SELECT 1 AS x FROM beziehungen WHERE von = 'Max Muster' AND relation = 'war_in' AND zu = 'Café Luitpold'")

        r = await g.finden("Was war letzte Woche mit Max?")
        assert r["verstanden"]["personen"] == ["Max Muster"] and r["verstanden"]["zeitraum"] == "letzte Woche"
        assert [x["text"][:14] for x in r["treffer"]] == ["Termin: Kaffee"]
        assert "fremder_inhalt" not in r  # eigener Kalender ist kein fremder Inhalt

        r = await g.finden("Fotos in München")
        assert r["anzahl"] == 7 and all("München" in x["text"] for x in r["treffer"])  # 5 Fotos, Fototag, Rückblick
        assert sum("bild" in x for x in r["treffer"]) == 6 and r["treffer"][1]["bild"].startswith("/api/objekt/")
        assert "fremder_inhalt" not in r

        r = await g.finden("Mails", person="Lena Berg")
        assert r["anzahl"] == 4  # 3 Mails + Rückblick: Lena wurde als Person angelegt und verknüpft
        assert await g.store.eine("SELECT 1 AS x FROM personen WHERE name = 'Lena Berg' AND kontakt->>'mail' = 'lena@example.org'")
        assert not await g.store.eine("SELECT 1 AS x FROM personen WHERE name ILIKE '%noreply%'")

        r = await g.finden("", person="Max Muster")
        assert {"kalender", "mail"} <= {x["quelle"] for x in r["treffer"]}  # Mail über die bekannte Adresse

        gew = await g.store.eine("SELECT text FROM erinnerungen WHERE art = 'praeferenz'")
        assert gew["text"] == "Gewohnheit: Training meist Dienstags um 19:00 Uhr"

        tl = await g.timeline(von=(datetime.now(TZ) - timedelta(days=4)).date().isoformat(), bis=datetime.now(TZ).date().isoformat())
        assert any(x["text"].startswith("Foto in München") for x in tl) and any("Mail von Lena" in x["text"] for x in tl)
        assert await g.store.eine("SELECT 1 AS x FROM erinnerungen WHERE quelle = 'rueckblick' AND text LIKE '%%5 Mails%%'")

        # Unbekannter Ort: Textsuche statt nichts
        assert (await g.finden("", ort="Luitpold"))["anzahl"] >= 1
        # Stichwortsuche ohne Filter
        assert (await g.finden("Fliesen"))["treffer"][0]["text"].startswith("Mail von Lena")
        # Vergessen löscht auch den Vektor
        eid = (await g.finden("Fliesen 2"))["treffer"][0]["id"]
        await g.vergessen(eid)
        assert not await g.store.eine("SELECT 1 AS x FROM erinnerungen WHERE id = %s", (eid,))
        assert eid not in [p.id for p in (await g.vek.client.scroll("erinnerungen", limit=500))[0]]

    asyncio.run(lauf())


def test_memchip_export_import(welt, dsn):
    g, org, chip, tmp = welt

    async def lauf():
        assert (await chip.status())["eingesteckt"] is False
        (tmp / "chip").mkdir()
        await g.orte_speichern("München", 48.137, 11.575, radius_m=5000)
        await org.lauf()
        await g.speichern("Max mag Kaffee schwarz", "praeferenz", personen=["Max Muster"])
        vorher = sql(dsn, "SELECT count(*) FROM erinnerungen")[0][0]
        datei = await chip.exportieren("test")
        assert datei.endswith(".nxm") and (tmp / "chip" / datei).read_bytes()[:6] == b"NXMEM1"
        assert b"Kaffee" not in (tmp / "chip" / datei).read_bytes()  # verschlüsselt

        # Zusammenführen auf sich selbst: nichts doppelt
        assert "neu: nichts" in await chip.importieren(datei)
        assert sql(dsn, "SELECT count(*) FROM erinnerungen")[0][0] == vorher

        # Ersetzen: altes wird gesichert, danach ist alles aus der Datei wieder da, mit Personen und Orten
        text = await chip.importieren(datei, "ersetzen")
        assert text.startswith("Gedächtnis ersetzt") and "vor-ersetzen" in text
        assert sql(dsn, "SELECT count(*) FROM erinnerungen")[0][0] == vorher
        assert sql(dsn, """SELECT p.name FROM erinnerungen e JOIN personen p ON p.id = e.person_id
                           WHERE e.text = 'Max mag Kaffee schwarz'""") == [("Max Muster",)]
        assert sql(dsn, "SELECT count(*) FROM fotos WHERE ort_id IS NOT NULL")[0][0] == 5
        await asyncio.sleep(0.2)
        await g.nachholen(500)
        r = await g.finden("Kaffee schwarz")
        assert r["treffer"][0]["text"] == "Max mag Kaffee schwarz"
        bild = next(x["bild"] for x in (await g.finden("Fotos in München"))["treffer"] if "bild" in x)
        assert chip.objekte.holen(bild.removeprefix("/api/objekt/"))[:2] == b"\xff\xd8"  # JPEG aus dem Chip

        # Falsches Passwort
        falsch = Memchip(g, chip.objekte, verzeichnis=str(tmp / "chip"), passwort="falsch")
        with pytest.raises(ValueError, match="Passwort"):
            await falsch.importieren(datei)
        assert (await chip.status())["sicherungen"][0]["datei"].endswith(".nxm")

    asyncio.run(lauf())


def test_kern_werkzeuge_und_policy(welt, dsn, tmp_path):
    """memory.find über den Hub: Treffer aus Kalender/Mail machen das Gespräch belastet."""
    pytest.importorskip("mcp")
    from qulsi_core.confirm import Freigaben, Notifier
    from qulsi_core.hub import Hub
    from qulsi_core.policy import Kontext, Policy
    from qulsi_core.tools import Kern

    g, org, _, _ = welt
    store = g.store
    kern = Kern(store, g.vek, Graph(store))
    assert {"memory.find", "memory.timeline", "memory.forget", "places.save", "memchip.import"} <= set(kern.werkzeuge)
    policy = Policy.load(ROOT / "config" / "policy.yaml")
    leer = tmp_path / "connectors"
    leer.mkdir()
    hub = Hub(str(leer), policy, Freigaben(dsn, "http://test", 300, Notifier("", "qulsi", "")), store, kern, "false")

    async def lauf():
        await hub.laden()
        await org.lauf()
        ctx = Kontext(kanal="app")
        text = await hub.aufrufen("memory_remember", {"text": "Anna mag Tulpen", "art": "praeferenz"}, ctx)
        assert "Gemerkt" in text and not ctx.belastet
        text = await hub.aufrufen("memory_find", {"frage": "Was mag Anna?"}, ctx)
        assert "Tulpen" in text and not ctx.belastet  # eigene Notiz: kein fremder Text
        text = await hub.aufrufen("memory_find", {"frage": "Was war letzte Woche mit Max?"}, ctx)
        assert "Kaffee" in text and not ctx.belastet  # eigener Termin: auch kein fremder Inhalt
        ctx2 = Kontext(kanal="stimme")
        text = await hub.aufrufen("memory_forget", {"id": 1}, ctx2)
        assert "Abgelehnt" in text or "Freigabe" in text  # high: nie direkt
        assert sql(dsn, "SELECT count(*) FROM erinnerungen WHERE id = 1")[0][0] == 1

    asyncio.run(lauf())


def test_suche_stellt_passendes_dokument_vor_erinnerungen(welt):
    """memory.search: ein Dokument, das besser passt als jede Erinnerung, steht vorne (unabhängig vom Organisator)."""
    g, org, _, _ = welt

    async def lauf():
        await org.lauf()  # Termin-Episoden liegen jetzt im Gedächtnis
        await g.vek.speichern("dokumente", 1, "Angebot Küchenstudio Berger Einbauküche Preis Montage",
                              {"titel": "Angebot_Kueche.md", "quelle": "datei:Angebot_Kueche.md"})
        r = await g.suchen("das Angebot vom Küchenstudio")
        assert r["fremder_inhalt"] is True and r["treffer"][0]["titel"] == "Angebot_Kueche.md"
        r = await g.suchen("Training")  # das Dokument passt nicht zur Frage: weder Treffer noch fremder Inhalt
        assert isinstance(r, list) and r[0]["text"].startswith(("Termin: Training", "Gewohnheit: Training"))
        assert all(t.get("art") != "dokument" for t in r)

    asyncio.run(lauf())


def test_memchip_ohne_passwort_verweigert(welt):
    g, _, chip, tmp = welt
    (tmp / "chip").mkdir()

    async def lauf():
        ohne = Memchip(g, chip.objekte, verzeichnis=str(tmp / "chip"), passwort="", unverschluesselt=False)
        s = await ohne.status()
        assert s["verschluesselung"] == "fehlt" and s["export_moeglich"] is False and "warnung" in s
        with pytest.raises(RuntimeError, match="MEMCHIP_PASSWORT"):
            await ohne.exportieren()
        ohne.auto_stunden = 1
        assert await ohne.automatisch() is None and not list((tmp / "chip").iterdir())
        bewusst = Memchip(g, chip.objekte, verzeichnis=str(tmp / "chip"), passwort="", unverschluesselt=True)
        assert (await bewusst.status())["verschluesselung"] == "unverschluesselt_erlaubt"
        assert (await bewusst.exportieren()).endswith(".tar")
        assert (await chip.status())["verschluesselung"] == "an"

    asyncio.run(lauf())
