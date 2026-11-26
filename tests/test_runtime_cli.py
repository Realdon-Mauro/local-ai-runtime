import json
import os
import stat

from lar.runtime_cli import main


def test_config_init_validate_and_show_exclude_secret(tmp_path, capsys, monkeypatch):
    path = tmp_path / "config.yaml"
    assert main(["config", "init", "--path", str(path)]) == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert main(["--config", str(path), "config", "validate"]) == 0
    assert "Configurazione valida" in capsys.readouterr().out

    path.write_text(path.read_text() + "api_token: local-secret\n", encoding="utf-8")
    assert main(["--config", str(path), "config", "show"]) == 0
    output = capsys.readouterr().out
    assert "api_token" not in output
    assert "local-secret" not in output

    assert main(["config", "init", "--path", str(path)]) == 1


def test_database_cli_migrate_check_and_backup_never_targets_legacy(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    database_path = tmp_path / "state" / "lar.sqlite3"
    config_path.write_text(f"database_path: {database_path}\n", encoding="utf-8")
    assert main(["--config", str(config_path), "db", "migrate"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    assert main(["--config", str(config_path), "db", "check"]) == 0
    assert json.loads(capsys.readouterr().out)["integrity"] == "ok"
    assert main(["--config", str(config_path), "db", "backup"]) == 0
    backup_path = json.loads(capsys.readouterr().out)["backup"]
    assert os.path.exists(backup_path)


def test_remote_serve_passes_tls_files_to_uvicorn(tmp_path, monkeypatch):
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("certificate fixture", encoding="utf-8")
    key.write_text("key fixture", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"bind_host: 0.0.0.0\ntls_cert_file: {cert}\ntls_key_file: {key}\napi_token: test-token\n",
        encoding="utf-8",
    )
    captured = {}
    monkeypatch.setattr("lar.runtime_cli.create_app", lambda config: "app")
    monkeypatch.setattr("lar.runtime_cli.uvicorn.run", lambda app, **kwargs: captured.update(app=app, **kwargs))
    assert main(["--config", str(config_path), "serve"]) == 0
    assert captured["host"] == "0.0.0.0"
    assert captured["ssl_certfile"] == str(cert)
    assert captured["ssl_keyfile"] == str(key)
    assert captured["app"] == "app"


def test_opencode_token_init_and_rotation_use_restrictive_permissions(tmp_path, capsys):
    token_path = tmp_path / "integrations" / "opencode.token"
    assert main(["integration", "opencode-token", "init", "--path", str(token_path)]) == 0
    first = token_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_path.parent.stat().st_mode) == 0o700
    assert main(["integration", "opencode-token", "init", "--path", str(token_path)]) == 1
    assert main(["integration", "opencode-token", "rotate", "--path", str(token_path)]) == 0
    assert token_path.read_text(encoding="utf-8") != first
    assert "Token OpenCode" in capsys.readouterr().out


def test_cli_requires_explicit_physical_stop_attestation(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    database_path = tmp_path / "runtime.sqlite3"
    config_path.write_text(f"database_path: {database_path}\n", encoding="utf-8")
    from lar.storage.database import Database

    database = Database(database_path)
    database.migrate()
    database.record_request("blocked-id", stream=False, tool_count=0)
    database.update_request(
        "blocked-id", status="provider_stop_unknown", error_code="provider_stop_unverified"
    )
    assert main([
        "--config", str(config_path), "safety", "confirm-provider-stopped",
        "--request-id", "blocked-id", "--i-verified-provider-stopped",
    ]) == 0
    assert "non ha verificato né arrestato Ollama" in capsys.readouterr().out
    assert not database.has_unresolved_provider_stop()
