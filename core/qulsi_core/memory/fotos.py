"""Fotos: Aufnahmezeit, GPS und Kamera aus EXIF lesen, Vorschaubild für den Object Storage."""

from __future__ import annotations

import io
from datetime import datetime, timezone, tzinfo

ENDUNGEN = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff"}
VORSCHAU_PX = 1280


def _grad(wert, ref: str | None) -> float | None:
    try:
        g, m, s = (float(x) for x in wert)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    d = g + m / 60 + s / 3600
    return -d if ref in ("S", "W") else d


def _bild_oeffnen(daten: bytes):
    from PIL import Image

    try:  # HEIC vom iPhone, wenn pillow-heif installiert ist
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    return Image.open(io.BytesIO(daten))


def exif_lesen(daten: bytes, tz: tzinfo = timezone.utc) -> dict:
    """{"aufgenommen", "lat", "lon", "kamera", "breite", "hoehe"}; fehlende Werte sind None."""
    bild = _bild_oeffnen(daten)
    out: dict = {"aufgenommen": None, "lat": None, "lon": None, "kamera": None, "breite": bild.width, "hoehe": bild.height}
    exif = bild.getexif()
    if not exif:
        return out
    ifd = exif.get_ifd(0x8769)  # Exif-IFD: DateTimeOriginal, OffsetTimeOriginal
    roh = ifd.get(36867) or exif.get(306)
    if isinstance(roh, str):
        try:
            ts = datetime.strptime(roh.strip()[:19], "%Y:%m:%d %H:%M:%S")
            versatz = ifd.get(36881)
            if isinstance(versatz, str) and len(versatz) >= 6:
                ts = datetime.fromisoformat(ts.isoformat() + versatz.strip()[:6])
            else:
                ts = ts.replace(tzinfo=tz)  # Kamerazeit ohne Zone: Ortszeit der Box annehmen
            out["aufgenommen"] = ts
        except ValueError:
            pass
    gps = exif.get_ifd(0x8825)
    if gps and 2 in gps and 4 in gps:
        lat, lon = _grad(gps[2], gps.get(1)), _grad(gps[4], gps.get(3))
        if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0.0, 0.0):
            out["lat"], out["lon"] = round(lat, 6), round(lon, 6)
    marke = " ".join(str(exif.get(t, "")).strip("\x00 ") for t in (271, 272)).strip()
    # Nur kurz und druckbar übernehmen: EXIF ist fremder Text
    out["kamera"] = "".join(ch for ch in marke if ch.isprintable())[:40] or None
    return out


def vorschau(daten: bytes, px: int = VORSCHAU_PX) -> bytes:
    """Verkleinertes JPEG ohne EXIF (GPS bleibt nur in der Datenbank)."""
    from PIL import ImageOps

    bild = ImageOps.exif_transpose(_bild_oeffnen(daten))
    bild.thumbnail((px, px))
    puffer = io.BytesIO()
    bild.convert("RGB").save(puffer, "JPEG", quality=82)
    return puffer.getvalue()
