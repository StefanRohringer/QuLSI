"""Memchip: das Gedächtnis als eine Datei auf einem austauschbaren Speicher (USB-Stick, SSD, SD-Karte).

Der Chip wird unter MEMCHIP_DIR eingebunden (Standard /data/memchip). Ein Export enthält
Personen, Projekte, Aufgaben, Orte, Erinnerungen, Wissensgraph, Fotos (Vorschaubilder) und
die zugehörigen Dateien aus dem Object Storage. Vektoren sind nicht drin: sie werden beim
Import neu berechnet, damit der Chip auch mit einem anderen Embedding-Modell funktioniert.

Die Datei wird mit MEMCHIP_PASSWORT verschlüsselt (AES-256-GCM, Schlüssel per scrypt). Ohne
Passwort verweigert QuLSI den Export. Nur mit MEMCHIP_UNVERSCHLUESSELT=1 landet bewusst ein
normales tar auf dem Chip.

Import "zusammenfuehren" fügt hinzu, was fehlt. "ersetzen" sichert erst das aktuelle
Gedächtnis auf den Chip und lädt dann nur den Chip (anderes Profil, neue Box).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import struct
import tarfile
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from .gedaechtnis import Gedaechtnis
from .objects import ObjectStore

log = logging.getLogger("memchip")
FORMAT = 1
MAGIC = b"NXMEM1"
BLOCK = 1024 * 1024
TABELLEN = {
    "personen": "SELECT id, name, aliase, beziehung, kontakt, notizen, erstellt FROM personen",
    "projekte": "SELECT id, name, beschreibung, status, oberprojekt, erstellt FROM projekte",
    "aufgaben": "SELECT id, titel, projekt_id, faellig, status, erstellt, erledigt FROM aufgaben",
    "orte": "SELECT id, name, aliase, art, lat, lon, radius_m, erstellt FROM orte",
    "erinnerungen": """SELECT id, art, text, person_id, projekt_id, ort_id, zeit, quelle, quelle_ref, objekt_key, daten, fremd
                       FROM erinnerungen""",
    "erinnerung_personen": "SELECT erinnerung_id, person_id FROM erinnerung_personen",
    "beziehungen": "SELECT von_typ, von, relation, zu_typ, zu, zeit FROM beziehungen",
    "fotos": """SELECT quelle, objekt_key, aufgenommen, lat, lon, ort_id, kamera, breite, hoehe, erinnerung_id, erstellt
                FROM fotos""",
}
KEY_RE = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{64}(\.[a-z0-9]{1,8})?$")


# ---------------------------------------------------------------------- Verschlüsselung (Datei in Blöcken)


def _schluessel(passwort: str, salz: bytes) -> bytes:
    return hashlib.scrypt(passwort.encode(), salt=salz, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)


def verschluesseln(quelle: Path, ziel: Path, passwort: str) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salz, praefix = os.urandom(16), os.urandom(7)
    aes = AESGCM(_schluessel(passwort, salz))
    groesse = quelle.stat().st_size
    with quelle.open("rb") as ein, ziel.open("wb") as aus:
        aus.write(MAGIC + salz + praefix)
        nr, gelesen = 0, 0
        while True:
            block = ein.read(BLOCK)
            gelesen += len(block)
            letzter = gelesen >= groesse
            nonce = praefix + struct.pack(">I", nr) + (b"\x01" if letzter else b"\x00")
            c = aes.encrypt(nonce, block, MAGIC)
            aus.write(struct.pack(">I", len(c)) + c)
            nr += 1
            if letzter:
                break


def entschluesseln(quelle: Path, ziel: Path, passwort: str) -> None:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    with quelle.open("rb") as ein, ziel.open("wb") as aus:
        kopf = ein.read(len(MAGIC) + 16 + 7)
        if not kopf.startswith(MAGIC):
            raise ValueError("Keine verschlüsselte QuLSI-Gedächtnisdatei.")
        salz, praefix = kopf[len(MAGIC):len(MAGIC) + 16], kopf[len(MAGIC) + 16:]
        aes = AESGCM(_schluessel(passwort, salz))
        nr = 0
        while True:
            laenge = ein.read(4)
            if len(laenge) < 4:
                raise ValueError("Datei ist unvollständig (abgeschnitten).")
            c = ein.read(struct.unpack(">I", laenge)[0])
            for flag in (b"\x00", b"\x01"):
                try:
                    block = aes.decrypt(praefix + struct.pack(">I", nr) + flag, c, MAGIC)
                    break
                except InvalidTag:
                    block = None
            if block is None:
                raise ValueError("Falsches Passwort oder beschädigte Datei.")
            aus.write(block)
            nr += 1
            if flag == b"\x01":
                return


def ist_verschluesselt(pfad: Path) -> bool:
    with pfad.open("rb") as f:
        return f.read(len(MAGIC)) == MAGIC


# ---------------------------------------------------------------------- Memchip


def _json(wert):
    if isinstance(wert, (datetime, date)):
        return wert.isoformat()
    raise TypeError(type(wert))


class Memchip:
    def __init__(self, g: Gedaechtnis, objekte: ObjectStore | None = None, verzeichnis: str | None = None,
                 passwort: str | None = None, unverschluesselt: bool | None = None):
        self.g = g
        self.store = g.store
        self.objekte = objekte or ObjectStore()
        self.dir = Path(verzeichnis or os.environ.get("MEMCHIP_DIR", "/data/memchip"))
        self.passwort = passwort if passwort is not None else os.environ.get("MEMCHIP_PASSWORT", "")
        # Ohne Passwort wird nichts exportiert, außer es ist ausdrücklich erlaubt (MEMCHIP_UNVERSCHLUESSELT=1)
        self.unverschluesselt = (unverschluesselt if unverschluesselt is not None
                                 else os.environ.get("MEMCHIP_UNVERSCHLUESSELT", "") == "1")
        self.auto_stunden = float(os.environ.get("MEMCHIP_AUTO_STUNDEN", "0"))
        self.behalten = int(os.environ.get("MEMCHIP_BEHALTEN", "5"))
        self._sperre = asyncio.Lock()

    @staticmethod
    def testumgebung() -> bool:
        """RunPod: der Ordner liegt auf dem Netzspeicher des Pods, das ist kein Memchip."""
        return os.environ.get("QULSI_LAUFZEIT") == "runpod" and os.environ.get("QULSI_MEMCHIP_RUNPOD") != "an"

    def eingesteckt(self) -> bool:
        return not self.testumgebung() and self.dir.is_dir() and os.access(self.dir, os.W_OK)

    def dateien(self) -> list[Path]:
        if not self.dir.is_dir():
            return []
        return sorted((p for p in self.dir.iterdir() if p.is_file() and p.suffix in (".nxm", ".tar")),
                      key=lambda p: p.stat().st_mtime, reverse=True)

    def verschluesselung(self) -> dict:
        """an: Export verschlüsselt. unverschluesselt_erlaubt: bewusst ohne Passwort. fehlt: Export wird verweigert."""
        if self.passwort:
            return {"verschluesselung": "an", "verschluesselt": True, "export_moeglich": True}
        if self.unverschluesselt:
            return {"verschluesselung": "unverschluesselt_erlaubt", "verschluesselt": False, "export_moeglich": True,
                    "warnung": "Das Gedächtnis liegt unverschlüsselt auf dem Memchip. Wer ihn findet, kann alles lesen."}
        return {"verschluesselung": "fehlt", "verschluesselt": False, "export_moeglich": False,
                "warnung": "MEMCHIP_PASSWORT ist nicht gesetzt. Ohne Passwort wird nichts auf den Memchip geschrieben."}

    async def status(self) -> dict:
        if self.testumgebung():
            return {"eingesteckt": False, "vorhanden": False, **self.verschluesselung(),
                    "hinweis": "Kein Memchip: Testumgebung (RunPod)."}
        if not self.eingesteckt():
            return {"eingesteckt": False, **self.verschluesselung(), "hinweis": f"Kein beschreibbarer Speicher unter {self.dir}."}
        frei = shutil.disk_usage(self.dir)
        return {
            "eingesteckt": True, "ordner": str(self.dir), "frei_gb": round(frei.free / 1e9, 1),
            **self.verschluesselung(), "automatisch_alle_stunden": self.auto_stunden or None,
            "sicherungen": [{"datei": p.name, "mb": round(p.stat().st_size / 1e6, 1),
                             "zeit": self.g.zeit(datetime.fromtimestamp(p.stat().st_mtime, timezone.utc))}
                            for p in self.dateien()[:20]],
        }

    # ------------------------------------------------------------------ Export

    async def exportieren(self, name: str | None = None, praefix: str = "") -> str:
        if not self.passwort and not self.unverschluesselt:
            raise RuntimeError("Export abgelehnt: MEMCHIP_PASSWORT ist nicht gesetzt. Das Gedächtnis wird nur verschlüsselt "
                               "auf den Memchip geschrieben (bewusst unverschlüsselt nur mit MEMCHIP_UNVERSCHLUESSELT=1).")
        if not self.eingesteckt():
            raise RuntimeError(f"Kein Memchip unter {self.dir} eingesteckt oder nicht beschreibbar.")
        await self.g.bereit()
        async with self._sperre:
            daten = {t: await self.store.alle(sql) for t, sql in TABELLEN.items()}
            keys = sorted({k for t in ("erinnerungen", "fotos") for r in daten[t] if (k := r.get("objekt_key"))})
            stempel = datetime.now(self.g.tz).strftime("%Y%m%d-%H%M%S")
            sauber = re.sub(r"[^\w-]", "_", name)[:40] if name else ""
            datei = self.dir / f"{praefix}qulsi-gedaechtnis-{stempel}{'-' + sauber if sauber else ''}{'.nxm' if self.passwort else '.tar'}"
            await asyncio.to_thread(self._schreiben, daten, keys, datei)
            await self.store.ereignis("memchip", f"Gedächtnis auf Memchip gesichert: {datei.name}", "core:gedaechtnis")
            return datei.name

    def _schreiben(self, daten: dict[str, list[dict]], keys: list[str], ziel: Path) -> None:
        manifest = {"format": FORMAT, "art": "qulsi-gedaechtnis", "erstellt": datetime.now(timezone.utc).isoformat(),
                    "tabellen": {t: len(r) for t, r in daten.items()}, "objekte": 0}
        # Unverschlüsseltes tar nur lokal (nicht auf dem Chip) zwischenlagern
        with tempfile.TemporaryDirectory(prefix="memchip-") as tmp:
            tar_pfad = Path(tmp) / "export.tar"
            with tarfile.open(tar_pfad, "w") as tar:
                def rein(name: str, inhalt: bytes) -> None:
                    info = tarfile.TarInfo(name)
                    info.size, info.mtime = len(inhalt), int(datetime.now().timestamp())
                    tar.addfile(info, io.BytesIO(inhalt))

                for t, rows in daten.items():
                    rein(f"tabellen/{t}.jsonl", "".join(json.dumps(r, default=_json, ensure_ascii=False) + "\n" for r in rows).encode())
                for k in keys:
                    try:
                        rein(f"objekte/{k}", self.objekte.holen(k))
                        manifest["objekte"] += 1
                    except (OSError, ValueError) as exc:
                        log.warning("Objekt %s fehlt: %s", k, exc)
                rein("manifest.json", json.dumps(manifest, indent=1).encode())
            teil = ziel.with_name(ziel.name + ".teil")
            if self.passwort:
                verschluesseln(tar_pfad, teil, self.passwort)
            else:
                shutil.copyfile(tar_pfad, teil)
            teil.replace(ziel)  # erst fertig, dann sichtbar: Chip ziehen mittendrin hinterlässt nur .teil

    async def automatisch(self) -> str | None:
        """Vom Organisator aufgerufen: regelmäßige Sicherung, alte automatische werden gelöscht."""
        if self.auto_stunden <= 0 or not self.eingesteckt() or not self.verschluesselung()["export_moeglich"]:
            return None
        auto = [p for p in self.dateien() if p.name.startswith("auto-")]
        if auto and datetime.now().timestamp() - auto[0].stat().st_mtime < self.auto_stunden * 3600:
            return None
        datei = await self.exportieren(praefix="auto-")
        for alt in [p for p in self.dateien() if p.name.startswith("auto-")][self.behalten:]:
            alt.unlink(missing_ok=True)
        return datei

    # ------------------------------------------------------------------ Import

    async def importieren(self, datei: str, modus: str = "zusammenfuehren") -> str:
        if modus not in ("zusammenfuehren", "ersetzen"):
            return "modus muss zusammenfuehren oder ersetzen sein."
        pfad = (self.dir / Path(datei).name)
        if not pfad.is_file():
            return f"Datei {datei} liegt nicht auf dem Memchip."
        with tempfile.TemporaryDirectory(prefix="memchip-") as tmp:
            tar_pfad = Path(tmp) / "import.tar"
            if ist_verschluesselt(pfad):
                if not self.passwort:
                    return "Die Datei ist verschlüsselt, aber MEMCHIP_PASSWORT ist nicht gesetzt."
                await asyncio.to_thread(entschluesseln, pfad, tar_pfad, self.passwort)
            else:
                tar_pfad = pfad
            inhalt = await asyncio.to_thread(self._lesen, tar_pfad)
        await self.g.bereit()
        if modus == "ersetzen":
            sicherung = await self.exportieren("vor-ersetzen")
            await self._leeren()
        async with self._sperre:
            zahlen = await self._einspielen(inhalt)
        self.g._vergessen_cache()
        asyncio.get_running_loop().create_task(self._alles_einbetten())
        await self.store.ereignis("memchip", f"Memchip {pfad.name} geladen ({modus})", "core:gedaechtnis", zahlen)
        text = ", ".join(f"{v} {k}" for k, v in zahlen.items() if v)
        if modus == "ersetzen":
            return f"Gedächtnis ersetzt durch {pfad.name}: {text}. Das vorherige liegt als {sicherung} auf dem Chip."
        return f"Memchip {pfad.name} zusammengeführt, neu: {text or 'nichts'}."

    def _lesen(self, tar_pfad: Path) -> dict:
        out: dict = {"tabellen": {}, "objekte": {}}
        with tarfile.open(tar_pfad, "r") as tar:
            for m in tar.getmembers():  # nichts entpacken, nur gezielt lesen (keine Pfade aus dem Archiv benutzen)
                if not m.isfile():
                    continue
                if m.name == "manifest.json":
                    out["manifest"] = json.loads(tar.extractfile(m).read())
                elif m.name.startswith("tabellen/") and m.name.endswith(".jsonl") and m.name[9:-6] in TABELLEN:
                    roh = tar.extractfile(m).read().decode()
                    out["tabellen"][m.name[9:-6]] = [json.loads(z) for z in roh.splitlines() if z.strip()]
                elif m.name.startswith("objekte/") and KEY_RE.match(m.name[8:]):
                    daten = tar.extractfile(m).read()
                    k = m.name[8:]
                    if hashlib.sha256(daten).hexdigest() == k[3:67]:  # inhaltsadressiert: Inhalt muss zum Namen passen
                        out["objekte"][k] = daten
        if (out.get("manifest") or {}).get("art") != "qulsi-gedaechtnis":
            raise ValueError("Keine QuLSI-Gedächtnisdatei.")
        if out["manifest"].get("format", 0) > FORMAT:
            raise ValueError("Die Datei stammt von einer neueren QuLSI-Version.")
        return out

    async def _leeren(self) -> None:
        async with await self.store.conn() as c:
            await c.execute("""TRUNCATE erinnerung_personen, fotos, erinnerungen, aufgaben, beziehungen, orte, projekte, personen
                               RESTART IDENTITY CASCADE""")
        try:
            await self.g.vek.client.delete_collection("erinnerungen")
            await self.g.vek.vorbereiten()
        except Exception as exc:  # noqa: BLE001
            log.warning("Qdrant-Collection nicht geleert: %s", exc)
        self.g._vergessen_cache()

    async def _einspielen(self, inhalt: dict) -> dict[str, int]:
        t = inhalt["tabellen"]
        z = {k: 0 for k in ("personen", "projekte", "orte", "aufgaben", "erinnerungen", "fotos", "beziehungen", "dateien")}
        for k, daten in inhalt["objekte"].items():
            try:
                await asyncio.to_thread(self.objekte.holen, k)  # schon da (inhaltsadressiert)
            except Exception:  # noqa: BLE001
                if await asyncio.to_thread(self.objekte.ablegen, daten, Path(k).suffix) == k:
                    z["dateien"] += 1
        async with await self.store.conn() as c:
            async with c.transaction():
                async def eine(sql, p):
                    return await (await c.execute(sql, p)).fetchone()

                pmap, prmap, omap, emap = {}, {}, {}, {}
                for p in t.get("personen", []):
                    r = await eine("SELECT id FROM personen WHERE lower(name) = lower(%s)", (p["name"],))
                    if not r:
                        r = await eine("""INSERT INTO personen (name, aliase, beziehung, kontakt, notizen, erstellt)
                                          VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                                       (p["name"], p["aliase"], p["beziehung"], json.dumps(p["kontakt"] or {}), p["notizen"], p["erstellt"]))
                        z["personen"] += 1
                    pmap[p["id"]] = r["id"]
                for p in t.get("projekte", []):
                    r = await eine("SELECT id FROM projekte WHERE lower(name) = lower(%s)", (p["name"],))
                    if not r:
                        r = await eine("INSERT INTO projekte (name, beschreibung, status, erstellt) VALUES (%s, %s, %s, %s) RETURNING id",
                                       (p["name"], p["beschreibung"], p["status"], p["erstellt"]))
                        z["projekte"] += 1
                    prmap[p["id"]] = r["id"]
                for p in t.get("projekte", []):
                    if p["oberprojekt"] in prmap:
                        await c.execute("UPDATE projekte SET oberprojekt = coalesce(oberprojekt, %s) WHERE id = %s",
                                        (prmap[p["oberprojekt"]], prmap[p["id"]]))
                for o in t.get("orte", []):
                    r = await eine("SELECT id FROM orte WHERE lower(name) = lower(%s)", (o["name"],))
                    if not r:
                        r = await eine("""INSERT INTO orte (name, aliase, art, lat, lon, radius_m, erstellt)
                                          VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                                       (o["name"], o["aliase"], o["art"], o["lat"], o["lon"], o["radius_m"], o["erstellt"]))
                        z["orte"] += 1
                    omap[o["id"]] = r["id"]
                for a in t.get("aufgaben", []):
                    prid = prmap.get(a["projekt_id"])
                    if not await eine("SELECT 1 AS x FROM aufgaben WHERE titel = %s AND erstellt = %s", (a["titel"], a["erstellt"])):
                        await c.execute("""INSERT INTO aufgaben (titel, projekt_id, faellig, status, erstellt, erledigt)
                                           VALUES (%s, %s, %s, %s, %s, %s)""",
                                        (a["titel"], prid, a["faellig"], a["status"], a["erstellt"], a["erledigt"]))
                        z["aufgaben"] += 1
                for e in t.get("erinnerungen", []):
                    ref = e["quelle_ref"] or f"import:{hashlib.sha256((e['text'] + e['zeit']).encode()).hexdigest()[:24]}"
                    da = await eine("SELECT id FROM erinnerungen WHERE quelle_ref = %s OR (text = %s AND zeit = %s)",
                                    (ref, e["text"], e["zeit"]))
                    if da:
                        emap[e["id"]] = da["id"]
                        continue
                    r = await eine(
                        """INSERT INTO erinnerungen (art, text, person_id, projekt_id, ort_id, zeit, quelle, quelle_ref, objekt_key, daten, fremd)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                        (e["art"], e["text"], pmap.get(e["person_id"]), prmap.get(e["projekt_id"]), omap.get(e["ort_id"]),
                         e["zeit"], e["quelle"], ref, e["objekt_key"], json.dumps(e["daten"] or {}), e["fremd"]))
                    emap[e["id"]] = r["id"]
                    z["erinnerungen"] += 1
                for ep in t.get("erinnerung_personen", []):
                    if ep["erinnerung_id"] in emap and ep["person_id"] in pmap:
                        await c.execute("INSERT INTO erinnerung_personen VALUES (%s, %s) ON CONFLICT DO NOTHING",
                                        (emap[ep["erinnerung_id"]], pmap[ep["person_id"]]))
                for b in t.get("beziehungen", []):
                    cur = await c.execute("""INSERT INTO beziehungen (von_typ, von, relation, zu_typ, zu, zeit)
                                             VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                                          (b["von_typ"], b["von"], b["relation"], b["zu_typ"], b["zu"], b["zeit"]))
                    z["beziehungen"] += cur.rowcount
                for f in t.get("fotos", []):
                    cur = await c.execute(
                        """INSERT INTO fotos (quelle, objekt_key, aufgenommen, lat, lon, ort_id, kamera, breite, hoehe, erinnerung_id, erstellt)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (quelle) DO NOTHING""",
                        (f["quelle"], f["objekt_key"], f["aufgenommen"], f["lat"], f["lon"], omap.get(f["ort_id"]), f["kamera"],
                         f["breite"], f["hoehe"], emap.get(f["erinnerung_id"]), f["erstellt"]))
                    z["fotos"] += cur.rowcount
        return z

    async def _alles_einbetten(self) -> None:
        try:
            while await self.g.nachholen(200):
                await asyncio.sleep(0)
        except Exception as exc:  # noqa: BLE001  der Organisator macht beim nächsten Lauf weiter
            log.warning("Einbetten nach Import unterbrochen: %s", exc)
