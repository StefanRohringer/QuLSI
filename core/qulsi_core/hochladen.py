"""Dateien und Fotos aus der App hochladen.

- GET    /api/hochladen?limit=10   Grenzen (Größe, Typen) und die letzten hochgeladenen Dateien
- POST   /api/hochladen            multipart, Feld „datei“: prüft Größe, MIME und Magic Bytes
- DELETE /api/hochladen/{id}       Datei, Vorschau und Gedächtnis-Eintrag wieder weg

Fotos landen in FOTOS_DIR/Hochgeladen/JJJJ-MM/ und werden sofort eingeordnet (EXIF, Ort, Vorschau),
Dokumente in FILES_DIR/Hochgeladen/JJJJ-MM/, danach indiziert der Dateien-Connector sie (files.index).
Gespeichert wird unter eigener ID, der Dateiname ist nur Anzeige. Alles Hochgeladene gilt als fremd:
ein Gespräch mit Anhang ist belastet, Dokumente liest das Modell über files_read (mit Injektionsprüfung).
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import re
import secrets
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse


log = logging.getLogger(__name__)
router = APIRouter()

ID_RE = re.compile(r"^h_[a-z0-9]{10}$")
ZU_GROSS = "Zu groß, höchstens {mb} MB."
TYP_FALSCH = "Diesen Dateityp nimmt QuLSI nicht an."


@dataclass(frozen=True)
class Typ:
    endung: str
    mime: str
    art: str          # foto | dokument
    titel: str        # für „Datei sieht nicht aus wie ein PDF“
    lesbar: bool = True  # kann files_read den Inhalt lesen?


TYPEN = {t.endung: t for t in [
    Typ(".jpg", "image/jpeg", "foto", "ein JPEG-Foto"), Typ(".png", "image/png", "foto", "ein PNG-Bild"),
    Typ(".webp", "image/webp", "foto", "ein WebP-Bild"), Typ(".heic", "image/heic", "foto", "ein HEIC-Foto"),
    Typ(".heif", "image/heif", "foto", "ein HEIF-Foto"), Typ(".gif", "image/gif", "foto", "ein GIF-Bild"),
    Typ(".pdf", "application/pdf", "dokument", "ein PDF"), Typ(".txt", "text/plain", "dokument", "eine Textdatei"),
    Typ(".md", "text/markdown", "dokument", "eine Textdatei"), Typ(".csv", "text/csv", "dokument", "eine CSV-Datei"),
    Typ(".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "dokument",
        "ein Word-Dokument", lesbar=False),
    Typ(".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "dokument",
        "eine Excel-Tabelle", lesbar=False),
    Typ(".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation", "dokument",
        "eine PowerPoint-Präsentation", lesbar=False),
    Typ(".odt", "application/vnd.oasis.opendocument.text", "dokument", "ein OpenDocument-Text", lesbar=False),
    Typ(".ods", "application/vnd.oasis.opendocument.spreadsheet", "dokument", "eine OpenDocument-Tabelle",
        lesbar=False),
]}
ENDUNG_ALIAS = {".jpeg": ".jpg", ".markdown": ".md"}
MIME_ALIAS = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg", "image/heic-sequence": "image/heic",
              "image/heif-sequence": "image/heif", "text/x-markdown": "text/markdown", "application/csv": "text/csv",
              "text/comma-separated-values": "text/csv", "application/x-pdf": "application/pdf"}
UNBESTIMMT = {"", "application/octet-stream", "binary/octet-stream", "application/x-unknown"}
HEIF_MARKEN = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avci"}
OOXML_ORDNER = {".docx": "word/", ".xlsx": "xl/", ".pptx": "ppt/"}


def max_mb() -> int:
    try:
        return max(1, int(os.environ.get("HOCHLADEN_MAX_MB", "25")))
    except ValueError:
        return 25


def fotos_ordner() -> Path:
    if d := os.environ.get("HOCHLADEN_FOTOS_DIR"):
        return Path(d)
    return Path(os.environ.get("FOTOS_DIR", "/data/fotos")) / "Hochgeladen"


def dateien_ordner() -> Path | None:
    if d := os.environ.get("HOCHLADEN_DATEIEN_DIR"):
        return Path(d)
    if d := os.environ.get("FILES_DIR"):
        return Path(d) / "Hochgeladen"
    return None


def _zone():
    try:
        return ZoneInfo(os.environ.get("TZ") or "Europe/Berlin")
    except (KeyError, ValueError):
        return None


def name_saeubern(name: str | None) -> str:
    """Nur zur Anzeige: kein Pfad, keine Steuerzeichen, höchstens 120 Zeichen."""
    roh = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    sauber = "".join(ch for ch in roh if ch.isprintable() and ch not in '<>:"|?*').strip().strip(".")
    return sauber[:120] or "datei"


def _dateiname_teil(name: str) -> str:
    """Für den Dateinamen auf der Platte (Dateisuche nach Namen): nur einfache Zeichen."""
    stamm = Path(name).stem
    t = re.sub(r"[^\wäöüÄÖÜß-]+", "_", stamm, flags=re.UNICODE).strip("_")
    return t[:60] or "datei"


def _ist_text(daten: bytes) -> bool:
    if b"\x00" in daten[:65536]:
        return False
    for kodierung in ("utf-8-sig", "cp1252"):
        try:
            daten.decode(kodierung)
            return True
        except UnicodeDecodeError:
            continue
    return False


def _zip_passt(daten: bytes, endung: str) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(daten)) as z:
            namen = z.namelist()
            if endung in OOXML_ORDNER:
                return "[Content_Types].xml" in namen and any(n.startswith(OOXML_ORDNER[endung]) for n in namen)
            if "mimetype" not in namen:
                return False
            mime = z.read("mimetype").decode("ascii", "replace").strip()
            return mime == TYPEN[endung].mime
    except (zipfile.BadZipFile, KeyError, OSError, RuntimeError):
        return False


def magie_passt(daten: bytes, endung: str) -> bool:
    """Passt der Inhalt zur Endung? (Magic Bytes, bei Office-Dateien auch der ZIP-Inhalt)"""
    k = daten[:16]
    if endung == ".jpg":
        return k.startswith(b"\xff\xd8\xff")
    if endung == ".png":
        return k.startswith(b"\x89PNG\r\n\x1a\n")
    if endung == ".gif":
        return k.startswith((b"GIF87a", b"GIF89a"))
    if endung == ".webp":
        return k[:4] == b"RIFF" and k[8:12] == b"WEBP"
    if endung in (".heic", ".heif"):
        return k[4:8] == b"ftyp" and k[8:12] in HEIF_MARKEN
    if endung == ".pdf":
        return b"%PDF-" in daten[:1024]
    if endung in (".txt", ".md", ".csv"):
        return _ist_text(daten)
    if endung in (".docx", ".xlsx", ".pptx", ".odt", ".ods"):
        return k.startswith(b"PK\x03\x04") and _zip_passt(daten, endung)
    return False


def typ_bestimmen(name: str, mime: str | None, daten: bytes) -> Typ:
    """Endung, angegebener MIME-Typ und Inhalt müssen zusammenpassen. Sonst 415 oder 400 mit Klartext."""
    endung = Path(name).suffix.lower()
    endung = ENDUNG_ALIAS.get(endung, endung)
    mime = (mime or "").split(";")[0].strip().lower()
    mime = MIME_ALIAS.get(mime, mime)
    typ = TYPEN.get(endung)
    if typ is None and mime not in UNBESTIMMT:  # ohne passende Endung (z. B. „Foto“ vom Handy): nach MIME
        typ = next((t for t in TYPEN.values() if t.mime == mime), None)
    if typ is None:
        raise HTTPException(415, TYP_FALSCH)
    if typ.art == "foto":  # Handys benennen Fotos gern falsch: bei Bildern zählt der Inhalt
        if not (mime in UNBESTIMMT or mime.startswith("image/")):
            raise HTTPException(415, TYP_FALSCH)
        echt = next((t for t in TYPEN.values() if t.art == "foto" and magie_passt(daten, t.endung)), None)
        if echt is None:
            raise HTTPException(400, f"Datei sieht nicht aus wie {typ.titel}.")
        return echt
    if not (mime in UNBESTIMMT or mime == typ.mime or (typ.endung in (".txt", ".md", ".csv") and mime.startswith("text/"))):
        raise HTTPException(415, TYP_FALSCH)
    if not magie_passt(daten, typ.endung):
        raise HTTPException(400, f"Datei sieht nicht aus wie {typ.titel}.")
    return typ


async def _lesen(datei: UploadFile, grenze: int) -> bytes:
    """Höchstens grenze Bytes lesen; mehr heißt 413, ohne alles in den Speicher zu holen."""
    teile, n = [], 0
    while chunk := await datei.read(1024 * 1024):
        n += len(chunk)
        if n > grenze:
            raise HTTPException(413, ZU_GROSS.format(mb=max_mb()))
        teile.append(chunk)
    return b"".join(teile)


def _eintrag(row: dict) -> dict:
    return {"id": row["id"], "name": row["name"], "art": row["art"], "groesse": row["groesse"],
            "zeit": row["zeit"].isoformat() if row.get("zeit") else None, "status": row["status"],
            "vorschau": f"/api/objekt/{row['vorschau_key']}" if row.get("vorschau_key") else None,
            "hinweis": row.get("hinweis")}


def _fehler(code: int, text: str) -> JSONResponse:
    return JSONResponse({"fehler": text, "detail": text}, status_code=code)


# ------------------------------------------------------------------------------------------ Endpunkte

@router.get("/api/hochladen")
async def liste(request: Request, limit: int = Query(10, ge=1, le=100)):
    rows = await request.app.state.store.alle(
        "SELECT * FROM hochgeladen ORDER BY zeit DESC LIMIT %s", (limit,))
    typen = sorted({t.mime for t in TYPEN.values()})
    return {"grenzen": {"max_mb": max_mb(), "typen": typen, "endungen": sorted(TYPEN) + sorted(ENDUNG_ALIAS)},
            "dateien": [_eintrag(r) for r in rows]}


@router.post("/api/hochladen")
async def hochladen(request: Request, datei: UploadFile = File(...), ziel: str = Form("auto")):
    st = request.app.state
    name = name_saeubern(datei.filename)
    try:
        daten = await _lesen(datei, max_mb() * 1024 * 1024)
    except HTTPException as exc:
        return _fehler(exc.status_code, exc.detail)
    if not daten:
        return _fehler(400, "Die Datei ist leer.")
    try:
        typ = typ_bestimmen(name, datei.content_type, daten)
    except HTTPException as exc:
        return _fehler(exc.status_code, exc.detail)
    if typ.art == "dokument" and dateien_ordner() is None:
        return _fehler(503, "Dokumente kann diese Box noch nicht annehmen (kein Dateiordner eingerichtet).")
    if Path(name).suffix.lower() not in (typ.endung, *[a for a, z in ENDUNG_ALIAS.items() if z == typ.endung]):
        name = f"{name}{typ.endung}"  # „Foto“ ohne Endung vom Handy

    hid = "h_" + "".join(secrets.choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(10))
    monat = datetime.now(_zone()).strftime("%Y-%m")
    if typ.art == "foto":
        basis, datei_name = fotos_ordner(), f"{hid}{typ.endung}"
    else:
        basis, datei_name = dateien_ordner(), f"{hid}-{_dateiname_teil(name)}{typ.endung}"
    pfad = basis / monat / datei_name
    try:
        await asyncio.to_thread(_schreiben, pfad, daten)
    except OSError as exc:
        log.warning("Hochladen: %s nicht schreibbar: %s", pfad, exc)
        return _fehler(503, "Die Box kann die Datei gerade nicht speichern (Ordner nicht beschreibbar).")
    rel = f"Hochgeladen/{monat}/{datei_name}"

    status, hinweis, vorschau_key, erinnerung = "eingeordnet", None, None, None
    if typ.art == "foto":
        try:
            vorschau_key, erinnerung = await st.hub.kern.organisator.foto_einordnen(daten, rel)
        except Exception as exc:  # noqa: BLE001  z. B. HEIC ohne pillow-heif
            log.info("Foto %s ohne Vorschau: %s", rel, exc)
            hinweis = "Für dieses Format hat die Box keine Vorschau. Die Datei ist trotzdem gespeichert."
    elif typ.lesbar:
        status = "wartet"
    else:
        hinweis = "Den Inhalt dieses Dateityps kann QuLSI noch nicht lesen. Die Datei liegt im Ordner Hochgeladen."

    row = await st.store.eine(
        """INSERT INTO hochgeladen (id, name, art, mime, groesse, pfad, status, hinweis, vorschau_key, erinnerung_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (hid, name, typ.art, typ.mime, len(daten), rel, status, hinweis, vorschau_key, erinnerung))
    was = "Foto" if typ.art == "foto" else "Datei"
    await st.store.ereignis("datei", f"{was} hochgeladen: {name}", "core:hochladen",
                            {"id": hid, "art": typ.art, "datei": rel})
    if status == "wartet":
        aufgabe = asyncio.create_task(_indizieren(st, hid))
        st.hochladen_aufgaben = getattr(st, "hochladen_aufgaben", set())
        st.hochladen_aufgaben.add(aufgabe)
        aufgabe.add_done_callback(st.hochladen_aufgaben.discard)
    return _eintrag(row)


@router.delete("/api/hochladen/{hid}")
async def loeschen(request: Request, hid: str):
    st = request.app.state
    if not ID_RE.match(hid):
        raise HTTPException(404, "Nicht gefunden")
    row = await st.store.eine("DELETE FROM hochgeladen WHERE id = %s RETURNING *", (hid,))
    if not row:
        raise HTTPException(404, "Nicht gefunden")
    pfad = _pfad(row)
    if pfad:
        await asyncio.to_thread(pfad.unlink, missing_ok=True)
    if row["art"] == "foto":
        await st.store.eine("DELETE FROM fotos WHERE quelle = %s RETURNING 1", (f"foto:{row['pfad']}",))
        if row.get("erinnerung_id"):
            await st.hub.kern.organisator.g.vergessen(row["erinnerung_id"])
    elif st.hub.finden("files.index"):
        aufgabe = asyncio.create_task(st.hub.daten("files.index", {}, kanal="app"))  # entfernt den Index-Eintrag
        st.hochladen_aufgaben = getattr(st, "hochladen_aufgaben", set())
        st.hochladen_aufgaben.add(aufgabe)
        aufgabe.add_done_callback(st.hochladen_aufgaben.discard)
    return {"geloescht": hid}


def _schreiben(pfad: Path, daten: bytes) -> None:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    tmp = pfad.with_name(f".{pfad.name}.tmp")
    tmp.write_bytes(daten)
    tmp.replace(pfad)


def _pfad(row: dict) -> Path | None:
    teil = str(row["pfad"]).removeprefix("Hochgeladen/")
    basis = fotos_ordner() if row["art"] == "foto" else dateien_ordner()
    if basis is None or ".." in Path(teil).parts:
        return None
    return basis / teil


async def _indizieren(st, hid: str) -> None:
    """Dateien-Connector indiziert neue Dateien; danach gilt das Dokument als eingeordnet."""
    status, hinweis = "eingeordnet", None
    try:
        if not st.hub.finden("files.index"):
            raise RuntimeError("Dateien-Connector nicht erreichbar")
        await st.hub.daten("files.index", {}, kanal="app")
    except Exception as exc:  # noqa: BLE001
        log.warning("Hochgeladene Datei %s nicht indiziert: %s", hid, exc)
        hinweis = "Die Datei ist gespeichert, die Suche darin kommt beim nächsten Durchlauf dazu."
    await st.store.eine("UPDATE hochgeladen SET status = %s, hinweis = %s WHERE id = %s RETURNING 1",
                        (status, hinweis, hid))


# ------------------------------------------------------------------------------------------ Im Gespräch

async def anhaenge_einbauen(st, nachricht: str, ids: list) -> tuple[str | list, bool]:
    """Nachricht mit Anhängen fürs Modell: Fotos als Bild (Vorschau), Dokumente als Hinweis auf files_read.

    Gibt (content, hat_anhang) zurück. Unbekannte IDs werden still übergangen.
    """
    ids = [i for i in (ids if isinstance(ids, list) else []) if isinstance(i, str) and ID_RE.match(i)][:10]
    if not ids:
        return nachricht, False
    rows = await st.store.alle("SELECT * FROM hochgeladen WHERE id = ANY(%s)", (ids,))
    nach_id = {r["id"]: r for r in rows}
    zeilen, bilder = [], []
    for i in ids:
        r = nach_id.get(i)
        if not r:
            continue
        if r["art"] == "foto":
            zeilen.append(f"[Anhang: Foto {r['name']}, id {r['id']}]")
            if r.get("vorschau_key"):
                try:
                    daten = await asyncio.to_thread(st.objekte.holen, r["vorschau_key"])
                    bilder.append("data:image/jpeg;base64," + base64.b64encode(daten).decode())
                except (FileNotFoundError, ValueError, OSError):
                    pass
        elif TYPEN.get(Path(r["pfad"]).suffix.lower(), Typ("", "", "", "", False)).lesbar:
            zeilen.append(f"[Anhang: Dokument {r['name']}, id {r['id']}. Inhalt mit files_read lesen: "
                          f"datei=\"{r['pfad']}\"]")
        else:
            zeilen.append(f"[Anhang: Dokument {r['name']}, id {r['id']}. Den Inhalt dieses Dateityps kannst du "
                          "noch nicht lesen, sag das ehrlich.]")
    if not zeilen:
        return nachricht, False
    text = "\n".join([nachricht.strip(), *zeilen]).strip()
    if not bilder:
        return text, True
    return [{"type": "text", "text": text}, *({"type": "image_url", "image_url": {"url": u}} for u in bilder)], True
