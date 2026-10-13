"""Einstellungen aus Umgebungsvariablen (siehe .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.environ["DATABASE_URL"])
    qdrant_url: str = field(default_factory=lambda: _env("QDRANT_URL", "http://qdrant:6333"))
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", "http://litellm:4000/v1"))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY", "qulsi"))
    embed_dim: int = field(default_factory=lambda: int(_env("EMBED_DIM", "1024")))
    voice_url: str = field(default_factory=lambda: _env("VOICE_URL", "http://voice:8300").rstrip("/"))
    tts_url: str = field(default_factory=lambda: _env("TTS_DIENST_URL").rstrip("/"))  # lädt nach neuer Stimmprobe neu
    tts_chatterbox_url: str = field(default_factory=lambda: _env("TTS_CHATTERBOX_DIENST_URL").rstrip("/"))
    egress_url: str = field(default_factory=lambda: _env("EGRESS_URL", "http://egress:3128"))
    offline: str = field(default_factory=lambda: _env("QULSI_OFFLINE", "auto").lower())  # auto | true | false
    connectors_dir: str = field(default_factory=lambda: _env("CONNECTORS_DIR", "/connectors"))
    policy_path: str = field(default_factory=lambda: _env("POLICY_PATH", "/config/policy.yaml"))
    router_path: str = field(default_factory=lambda: _env("ROUTER_PATH", "/config/router.yaml"))
    prompt_path: str = field(default_factory=lambda: _env("PROMPT_PATH", "/config/agent_prompt.md"))
    # Modelle überschreiben ("aus" = Modell abschalten), sonst gilt config/router.yaml
    model_small: str | None = field(default_factory=lambda: os.environ.get("MODEL_SMALL"))
    model_standard: str | None = field(default_factory=lambda: os.environ.get("MODEL_STANDARD"))
    model_large: str | None = field(default_factory=lambda: os.environ.get("MODEL_LARGE"))
    public_url: str = field(default_factory=lambda: _env("PUBLIC_URL", "http://qulsi.local").rstrip("/"))
    api_key: str = field(default_factory=lambda: _env("QULSI_API_KEY"))
    ntfy_url: str = field(default_factory=lambda: _env("NTFY_URL").rstrip("/"))
    ntfy_topic: str = field(default_factory=lambda: _env("NTFY_TOPIC", "qulsi"))
    ntfy_token: str = field(default_factory=lambda: _env("NTFY_TOKEN"))
    # Ältere Einrichtung: PIN-Hash aus .env, wird beim Start einmalig in die Datenbank übernommen (sonst in der App setzen)
    freigabe_pin: str = field(default_factory=lambda: _env("QULSI_FREIGABE_PIN"))
    max_schritte: int = field(default_factory=lambda: int(_env("AGENT_MAX_STEPS", "6")))
    timezone: str = field(default_factory=lambda: _env("TZ", "Europe/Berlin"))
