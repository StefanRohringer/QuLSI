"""Hochladen aus der App: Typprüfung (Endung, MIME, Magic Bytes), Dateinamen, Anhänge im Gespräch."""

import asyncio
import io
import zipfile

import pytest
from fastapi import HTTPException

from qulsi_core import hochladen as h


def _zip(dateien: dict[str, bytes]) -> bytes:
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w") as z:
        for name, inhalt in dateien.items():
            z.writestr(name, inhalt)
    return puffer.getvalue()


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20


@pytest.mark.parametrize("name,mime,daten,endung", [
    ("urlaub.jpg", "image/jpeg", JPEG, ".jpg"),
    ("urlaub.JPEG", "image/jpg", JPEG, ".jpg"),
    ("IMG_0001.HEIC", "image/heic", HEIC, ".heic"),
    ("bild.jpg", "image/jpeg", PNG, ".png"),          # falsch benannt: der Inhalt zählt
    ("Foto", "image/png", PNG, ".png"),               # vom Handy ohne Endung
    ("rechnung.pdf", "application/pdf", b"%PDF-1.7\n...", ".pdf"),
    ("notiz.md", "text/markdown", "Größe: 2 m".encode(), ".md"),
    ("liste.csv", "application/vnd.ms-excel", b"a;b\n1;2\n", None),
    ("liste.csv", "text/csv", "a;b\nä;ö\n".encode("cp1252"), ".csv"),
    ("brief.docx", "", _zip({"[Content_Types].xml": b"x", "word/document.xml": b"x"}), ".docx"),
    ("tabelle.ods", "application/octet-stream", _zip({"mimetype": b"application/vnd.oasis.opendocument.spreadsheet"}), ".ods"),
])
def test_typ_erkannt(name, mime, daten, endung):
    if endung is None:
        with pytest.raises(HTTPException) as e:
            h.typ_bestimmen(name, mime, daten)
        assert e.value.status_code == 415
        return
    assert h.typ_bestimmen(name, mime, daten).endung == endung


@pytest.mark.parametrize("name,mime,daten,code", [
    ("tool.exe", "application/x-msdownload", b"MZ", 415),
    ("tool", "application/octet-stream", b"MZ", 415),
    ("rechnung.pdf", "application/pdf", b"<html>", 400),
    ("rechnung.pdf", "text/html", b"%PDF-1.4", 415),
    ("bild.jpg", "image/jpeg", b"<svg onload=alert(1)>", 400),
    ("bild.jpg", "text/html", JPEG, 415),
    ("brief.docx", "", _zip({"word/document.xml": b"x"}), 400),     # ohne [Content_Types].xml
    ("brief.docx", "", _zip({"[Content_Types].xml": b"x", "xl/x.xml": b"x"}), 400),  # Excel als Word
    ("notiz.txt", "text/plain", b"\x00\x01binary", 400),
])
def test_typ_abgelehnt(name, mime, daten, code):
    with pytest.raises(HTTPException) as e:
        h.typ_bestimmen(name, mime, daten)
    assert e.value.status_code == code


def test_name_nur_anzeige():
    assert h.name_saeubern("../../etc/passwd") == "passwd"
    assert h.name_saeubern("C:\\Users\\kim\\Bild 1.jpg") == "Bild 1.jpg"
    assert h.name_saeubern("a\x00b<c>.pdf") == "abc.pdf"
    assert h.name_saeubern("") == "datei" and h.name_saeubern("...") == "datei"
    assert h._dateiname_teil("Notiz Garten (alt).md") == "Notiz_Garten_alt"


class _Datei:
    def __init__(self, groesse: int):
        self.rest = groesse

    async def read(self, n: int) -> bytes:
        k = min(n, self.rest)
        self.rest -= k
        return b"x" * k


def test_zu_gross(monkeypatch):
    monkeypatch.setenv("HOCHLADEN_MAX_MB", "1")
    assert len(asyncio.run(h._lesen(_Datei(1024 * 1024), 1024 * 1024))) == 1024 * 1024
    with pytest.raises(HTTPException) as e:
        asyncio.run(h._lesen(_Datei(1024 * 1024 + 1), 1024 * 1024))
    assert e.value.status_code == 413 and "1 MB" in e.value.detail


def test_ordner(monkeypatch, tmp_path):
    monkeypatch.delenv("HOCHLADEN_FOTOS_DIR", raising=False)
    monkeypatch.delenv("HOCHLADEN_DATEIEN_DIR", raising=False)
    monkeypatch.delenv("FILES_DIR", raising=False)
    monkeypatch.setenv("FOTOS_DIR", str(tmp_path))
    assert h.fotos_ordner() == tmp_path / "Hochgeladen" and h.dateien_ordner() is None
    monkeypatch.setenv("FILES_DIR", str(tmp_path / "f"))
    assert h.dateien_ordner() == tmp_path / "f" / "Hochgeladen"


class _Store:
    def __init__(self, rows):
        self.rows = rows

    async def alle(self, sql, args):
        return [r for r in self.rows if r["id"] in args[0]]


class _Objekte:
    def holen(self, key):
        return JPEG


class _St:
    def __init__(self, rows):
        self.store, self.objekte = _Store(rows), _Objekte()


ROWS = [{"id": "h_aaaaaaaaaa", "name": "urlaub.jpg", "art": "foto", "pfad": "Hochgeladen/2026-10/h_aaaaaaaaaa.jpg",
         "vorschau_key": "ab/abc.jpg"},
        {"id": "h_bbbbbbbbbb", "name": "Notiz.md", "art": "dokument", "pfad": "Hochgeladen/2026-10/h_bbbbbbbbbb-Notiz.md",
         "vorschau_key": None},
        {"id": "h_cccccccccc", "name": "Brief.docx", "art": "dokument",
         "pfad": "Hochgeladen/2026-10/h_cccccccccc-Brief.docx", "vorschau_key": None}]


def test_anhaenge_im_gespraech():
    st = _St(ROWS)
    inhalt, anhang = asyncio.run(h.anhaenge_einbauen(st, "Was ist das?", ["h_aaaaaaaaaa", "h_bbbbbbbbbb", "h_cccccccccc"]))
    assert anhang and isinstance(inhalt, list)
    text = inhalt[0]["text"]
    assert text.startswith("Was ist das?") and "[Anhang: Foto urlaub.jpg" in text
    assert 'files_read lesen: datei="Hochgeladen/2026-10/h_bbbbbbbbbb-Notiz.md"' in text
    assert "Brief.docx" in text and "noch nicht lesen" in text
    assert inhalt[1]["type"] == "image_url" and inhalt[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_anhaenge_unbekannt_oder_kaputt():
    st = _St(ROWS)
    assert asyncio.run(h.anhaenge_einbauen(st, "Hallo", [])) == ("Hallo", False)
    assert asyncio.run(h.anhaenge_einbauen(st, "Hallo", "h_aaaaaaaaaa")) == ("Hallo", False)
    assert asyncio.run(h.anhaenge_einbauen(st, "Hallo", ["../x", 5, "h_zzzzzzzzzz"])) == ("Hallo", False)
    inhalt, anhang = asyncio.run(h.anhaenge_einbauen(st, "", ["h_bbbbbbbbbb"]))
    assert anhang and isinstance(inhalt, str) and inhalt.startswith("[Anhang: Dokument Notiz.md")
