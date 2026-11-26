"""Validated LAR configuration with defaults → YAML → environment precedence."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    bind_host: str = "127.0.0.1"
    bind_port: int = Field(default=8000, ge=1, le=65535)
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_timeout_seconds: float = Field(default=300.0, gt=0)
    database_path: Path = Path("~/.local/state/lar/lar.sqlite3")
    queue_capacity: int = Field(default=10, ge=1, le=10)
    queue_timeout_seconds: float = Field(default=120.0, gt=0)
    request_timeout_seconds: float = Field(default=500.0, gt=0)
    # ADR-006 fissa questi valori: non sono policy host-specific.
    stop_check_interval_seconds: float = Field(default=5.0, ge=5.0, le=5.0)
    stop_confirmation_timeout_seconds: float = Field(default=60.0, ge=60.0, le=60.0)
    shutdown_grace_period_seconds: float = Field(default=30.0, ge=0)
    resource_ram_reserve_bytes: int = Field(default=0, ge=0)
    resource_vram_reserve_bytes: int = Field(default=0, ge=0)
    resource_disk_reserve_bytes: int = Field(default=0, ge=0)
    resource_max_cpu_percent: float | None = Field(default=None, ge=0, le=100)
    resource_max_temperature_c: float | None = Field(default=None, allow_inf_nan=False)
    resource_max_snapshot_age_seconds: float = Field(default=30.0, gt=0)
    resource_allow_constrained: bool = True
    retention_resource_days: int = Field(default=7, ge=1)
    retention_inference_metrics_days: int = Field(default=365, ge=1)
    retention_events_days: int = Field(default=14, ge=1)
    retention_saved_content_days: int = Field(default=90, ge=1)
    cors_origins: tuple[str, ...] = ()
    api_token: str | None = None
    opencode_token_file: Path = Path("~/.config/lar/integrations/opencode.token")
    tls_cert_file: Path | None = None
    tls_key_file: Path | None = None

    @field_validator("schema_version")
    @classmethod
    def supported_schema(cls, value: int) -> int:
        if value != 1:
            raise ValueError(f"Versione config non supportata: {value}")
        return value

    def effective_hash(self) -> str:
        """Hash deterministico dei soli valori non segreti."""
        values = self.model_dump(mode="json", exclude={"api_token"})
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def safe_snapshot(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"api_token"})


def _default_path() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    root = Path(configured).expanduser() if configured else Path.home() / ".config"
    return root / "lar" / "config.yaml"


def _parse_bool(value: str) -> bool:
    normalized = value.casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("boolean must be true/false")


def load_config(
    path: Path | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> RuntimeConfig:
    """Load YAML and apply explicit LAR_* overrides; invalid values fail loudly."""
    source = path or _default_path()
    values: dict[str, Any] = {}
    if source.exists():
        with source.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ValueError("Il file di configurazione deve contenere una mappa YAML.")
        values.update(loaded)

    environment = os.environ if environ is None else environ
    env_fields = {
        "BIND_HOST": ("bind_host", str),
        "BIND_PORT": ("bind_port", int),
        "OLLAMA_URL": ("ollama_url", str),
        "OLLAMA_TIMEOUT_SECONDS": ("ollama_timeout_seconds", float),
        "DATABASE_PATH": ("database_path", str),
        "QUEUE_CAPACITY": ("queue_capacity", int),
        "QUEUE_TIMEOUT_SECONDS": ("queue_timeout_seconds", float),
        "REQUEST_TIMEOUT_SECONDS": ("request_timeout_seconds", float),
        "SHUTDOWN_GRACE_PERIOD_SECONDS": (
            "shutdown_grace_period_seconds",
            float,
        ),
        "RESOURCE_RAM_RESERVE_BYTES": ("resource_ram_reserve_bytes", int),
        "RESOURCE_VRAM_RESERVE_BYTES": ("resource_vram_reserve_bytes", int),
        "RESOURCE_DISK_RESERVE_BYTES": ("resource_disk_reserve_bytes", int),
        "RESOURCE_MAX_CPU_PERCENT": ("resource_max_cpu_percent", float),
        "RESOURCE_MAX_TEMPERATURE_C": ("resource_max_temperature_c", float),
        "RESOURCE_MAX_SNAPSHOT_AGE_SECONDS": (
            "resource_max_snapshot_age_seconds",
            float,
        ),
        "RESOURCE_ALLOW_CONSTRAINED": (
            "resource_allow_constrained",
            _parse_bool,
        ),
        "API_TOKEN": ("api_token", str),
        "OPENCODE_TOKEN_FILE": ("opencode_token_file", str),
        "TLS_CERT_FILE": ("tls_cert_file", str),
        "TLS_KEY_FILE": ("tls_key_file", str),
        "CORS_ORIGINS": ("cors_origins", lambda value: tuple(item for item in value.split(",") if item)),
    }
    for suffix, (field, converter) in env_fields.items():
        key = f"LAR_{suffix}"
        if key in environment:
            try:
                values[field] = converter(environment[key])
            except ValueError as exc:
                raise ValueError(f"Valore ambiente non valido: {key}") from exc
    config = RuntimeConfig.model_validate(values)
    if not config.bind_host.startswith("127.") and config.bind_host != "::1":
        try:
            loopback = ipaddress.ip_address(config.bind_host).is_loopback
        except ValueError:
            loopback = False
        if not loopback and (
            not config.api_token or not config.tls_cert_file or not config.tls_key_file
        ):
            raise ValueError("Bind non-loopback richiede API token e certificato/chiave TLS.")
        if not loopback:
            if not config.tls_cert_file.expanduser().is_file():
                raise ValueError("Il certificato TLS configurato non esiste o non è un file.")
            if not config.tls_key_file.expanduser().is_file():
                raise ValueError("La chiave TLS configurata non esiste o non è un file.")
    return config
