"""Object Storage für Bilder, PDFs, Audio. Standard: Dateisystem, optional S3 (MinIO, Garage, ...)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


class ObjectStore:
    """Inhaltsadressiert: der Schlüssel ist der SHA-256 der Daten, gleiche Dateien liegen nur einmal da."""

    def __init__(self, root: str | None = None, s3_url: str | None = None, bucket: str = "qulsi"):
        self.root = Path(root or os.environ.get("OBJECT_STORE_DIR", "/data/objects"))
        self.s3_url = s3_url or os.environ.get("S3_URL")
        self.bucket = os.environ.get("S3_BUCKET", bucket)
        self._s3 = None
        if self.s3_url:
            import boto3  # nur nötig, wenn S3 genutzt wird

            self._s3 = boto3.client("s3", endpoint_url=self.s3_url)

    @staticmethod
    def schluessel(daten: bytes, endung: str = "") -> str:
        h = hashlib.sha256(daten).hexdigest()
        return f"{h[:2]}/{h}{endung}"

    def ablegen(self, daten: bytes, endung: str = "") -> str:
        key = self.schluessel(daten, endung)
        if self._s3:
            self._s3.put_object(Bucket=self.bucket, Key=key, Body=daten)
        else:
            ziel = self.root / key
            if not ziel.exists():
                ziel.parent.mkdir(parents=True, exist_ok=True)
                tmp = ziel.with_suffix(ziel.suffix + ".tmp")
                tmp.write_bytes(daten)
                tmp.replace(ziel)
        return key

    def holen(self, key: str) -> bytes:
        if self._s3:
            return self._s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        pfad = (self.root / key).resolve()
        if self.root.resolve() not in pfad.parents:
            raise ValueError("ungültiger Schlüssel")
        return pfad.read_bytes()
