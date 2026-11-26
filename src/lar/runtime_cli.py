"""Operational CLI for Local AI Runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from pydantic import ValidationError

from lar.api.gateway import create_app
from lar.core.config import RuntimeConfig, load_config
from lar.core.resources import ResourceManager
from lar.providers.ollama import OllamaError, OllamaProvider
from lar.storage.database import Database, StorageError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lar", description="Local AI Runtime")
    parser.add_argument("--config", type=Path, help="Percorso config YAML")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("serve", help="Avvia il gateway LAR")
    commands.add_parser("status", help="Diagnostica provider, database e risorse")

    config = commands.add_parser("config", help="Gestione configurazione")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("validate", help="Valida YAML ed environment")
    config_commands.add_parser("show", help="Mostra configurazione effettiva senza segreti")
    initialize = config_commands.add_parser("init", help="Crea un file config locale di esempio")
    initialize.add_argument("--path", type=Path, help="Destinazione; default ~/.config/lar/config.yaml")

    database = commands.add_parser("db", help="Database runtime SQLite")
    database_commands = database.add_subparsers(dest="db_command", required=True)
    database_commands.add_parser("migrate", help="Applica migration additive al database runtime")
    database_commands.add_parser("check", help="Controlla integrità e foreign key")
    database_commands.add_parser("backup", help="Crea backup verificato del database runtime")
    database_commands.add_parser("prune", help="Applica la retention ADR-004 al database runtime")
    legacy_import = database_commands.add_parser(
        "import-legacy-benchmarks", help="Importa esplicitamente metadati dal DB legacy in sola lettura"
    )
    legacy_import.add_argument("--source", type=Path, required=True)

    integration = commands.add_parser("integration", help="Credenziali integrazione osservativa")
    integration_commands = integration.add_subparsers(dest="integration_command", required=True)
    token = integration_commands.add_parser("opencode-token", help="Crea o ruota il token locale OpenCode")
    token.add_argument("action", choices=("init", "rotate"))
    token.add_argument("--path", type=Path, help="Override per test o installazioni dedicate")

    safety = commands.add_parser("safety", help="Azioni operatore per stati provider non verificabili")
    safety_commands = safety.add_subparsers(dest="safety_command", required=True)
    confirm = safety_commands.add_parser(
        "confirm-provider-stopped", help="Registra una verifica manuale dello stop fisico Ollama"
    )
    confirm.add_argument("--request-id", required=True)
    confirm.add_argument("--i-verified-provider-stopped", action="store_true", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "config" and args.config_command == "init":
        return _init_config(args.path)
    try:
        config = load_config(args.config)
    except (ValidationError, ValueError, OSError, yaml.YAMLError) as exc:
        print(f"Configurazione non valida: {exc}", file=sys.stderr)
        return 2

    try:
        if args.command == "config":
            return _config_command(args.config_command, config)
        if args.command == "db":
            return _database_command(args.db_command, config, getattr(args, "source", None))
        if args.command == "status":
            return asyncio.run(_status(config))
        if args.command == "integration":
            path = args.path or config.opencode_token_file
            return _opencode_token(args.action, path)
        if args.command == "safety":
            return _confirm_provider_stopped(config, args.request_id)
        if args.command == "serve":
            return _serve(config)
    except (StorageError, OSError, ValueError) as exc:
        print(f"Errore LAR: {exc}", file=sys.stderr)
        return 1
    return 2


def _config_command(action: str, config: RuntimeConfig) -> int:
    if action == "validate":
        print(f"Configurazione valida (schema {config.schema_version}, hash {config.effective_hash()}).")
        return 0
    if action == "show":
        print(json.dumps(config.safe_snapshot(), indent=2, ensure_ascii=False, sort_keys=True))
        print(f"effective_config_hash={config.effective_hash()}")
        return 0
    return 2


def _init_config(path: Path | None) -> int:
    destination = path or _default_config_path()
    destination = destination.expanduser()
    if destination.exists():
        print(f"Configurazione già esistente: {destination}", file=sys.stderr)
        return 1
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.write_text(_CONFIG_EXAMPLE, encoding="utf-8")
    os.chmod(destination, 0o600)
    print(f"Creato {destination} (permessi 0600). Rivedere ollama_url e database_path.")
    return 0


def _opencode_token(action: str, path: Path) -> int:
    destination = path.expanduser()
    if action == "init" and destination.exists():
        print("Token già esistente; usare rotate per invalidarlo.", file=sys.stderr)
        return 1
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination.parent, 0o700)
    token = secrets.token_urlsafe(32)
    handle, temporary_name = tempfile.mkstemp(prefix=".opencode-token-", dir=destination.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            output.write(token + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, destination)
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    print(f"Token OpenCode {action} salvato in {destination} (permessi 0600).")
    return 0


def _confirm_provider_stopped(config: RuntimeConfig, request_id: str) -> int:
    database = Database(config.database_path)
    if not database.acknowledge_provider_stopped(request_id):
        print("Nessun blocco provider_stop_unknown corrispondente.", file=sys.stderr)
        return 1
    print(
        "Conferma operatore registrata. LAR non ha verificato né arrestato Ollama; "
        "riavviare il runtime solo dopo aver verificato lo stop fisico."
    )
    return 0


def _default_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return root / "lar" / "config.yaml"


_CONFIG_EXAMPLE = """schema_version: 1
bind_host: 127.0.0.1
bind_port: 8000
ollama_url: http://127.0.0.1:11434
ollama_timeout_seconds: 300
database_path: ~/.local/state/lar/lar.sqlite3
queue_capacity: 10
queue_timeout_seconds: 120
request_timeout_seconds: 500
stop_check_interval_seconds: 5
stop_confirmation_timeout_seconds: 60
shutdown_grace_period_seconds: 30
resource_ram_reserve_bytes: 0
resource_vram_reserve_bytes: 0
resource_disk_reserve_bytes: 0
resource_max_snapshot_age_seconds: 30
resource_allow_constrained: true
retention_resource_days: 7
retention_inference_metrics_days: 365
retention_events_days: 14
retention_saved_content_days: 90
# Impostare CORS e token solo se il client ne ha bisogno.
cors_origins: []
"""


def _database_command(action: str, config: RuntimeConfig, source: Path | None = None) -> int:
    database = Database(config.database_path)
    if action == "migrate":
        backup = database.migrate()
        print(json.dumps({"status": "ok", "backup": str(backup) if backup else None}))
        return 0
    if action == "check":
        print(json.dumps({"integrity": database.health()}))
        return 0
    if action == "backup":
        print(json.dumps({"backup": str(database.backup())}))
        return 0
    if action == "prune":
        print(json.dumps({"deleted": database.purge_expired(config)}))
        return 0
    if action == "import-legacy-benchmarks":
        if source is None:
            return 2
        print(json.dumps({"import": database.import_legacy_benchmarks(source)}))
        return 0
    return 2


def _serve(config: RuntimeConfig) -> int:
    cert = config.tls_cert_file.expanduser() if config.tls_cert_file else None
    key = config.tls_key_file.expanduser() if config.tls_key_file else None
    if cert and not cert.is_file():
        raise ValueError(f"Certificato TLS assente: {cert}")
    if key and not key.is_file():
        raise ValueError(f"Chiave TLS assente: {key}")
    app = create_app(config)
    uvicorn.run(
        app,
        host=config.bind_host,
        port=config.bind_port,
        ssl_certfile=str(cert) if cert else None,
        ssl_keyfile=str(key) if key else None,
        access_log=False,
    )
    return 0


async def _status(config: RuntimeConfig) -> int:
    provider = OllamaProvider(base_url=config.ollama_url, timeout=config.ollama_timeout_seconds)
    try:
        try:
            await provider.health_check()
            provider_status: dict[str, Any] = {"status": "healthy"}
        except OllamaError as exc:
            provider_status = {"status": "unavailable", "error": type(exc).__name__}
        database = Database(config.database_path)
        try:
            storage_status = database.health()
        except StorageError:
            storage_status = "not_initialized"
        resource = await ResourceManager(
            storage_path=config.database_path.expanduser().parent,
            provider=provider,
        ).snapshot()
        result = {
            "provider": provider_status,
            "storage": storage_status,
            "provider_stop_blocked": database.has_unresolved_provider_stop()
            if storage_status == "ok" else False,
            "resources": {
                "sources": resource.sources,
                "ram_total_bytes": resource.ram_total_bytes,
                "ram_available_bytes": resource.ram_available_bytes,
                "cpu_utilization_percent": resource.cpu_utilization_percent,
                "gpu_count": len(resource.gpus),
                "temperature_sensors": len(resource.temperatures),
            },
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if provider_status["status"] == "healthy" and storage_status == "ok" else 1
    finally:
        await provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
