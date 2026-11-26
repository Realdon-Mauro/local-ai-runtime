import pytest
from pydantic import ValidationError

from lar.core.config import RuntimeConfig, load_config


def test_defaults_yaml_and_environment_precedence(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "schema_version: 1\nqueue_capacity: 4\nbind_port: 8100\n", encoding="utf-8"
    )
    config = load_config(
        config_file,
        environ={"LAR_QUEUE_CAPACITY": "8", "LAR_BIND_PORT": "8200"},
    )
    assert config.queue_capacity == 8
    assert config.bind_port == 8200
    assert config.ollama_url == "http://127.0.0.1:11434"


def test_invalid_explicit_environment_and_unknown_yaml_fail(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("unknown_setting: true\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(config_file, environ={})
    config_file.write_text("schema_version: 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="LAR_BIND_PORT"):
        load_config(config_file, environ={"LAR_BIND_PORT": "invalid"})


def test_remote_bind_requires_token_and_tls_paths(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("bind_host: 0.0.0.0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="token.*TLS"):
        load_config(path, environ={})
    path.write_text(
        "bind_host: 0.0.0.0\ntls_cert_file: cert.pem\ntls_key_file: key.pem\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="token.*TLS"):
        load_config(path, environ={})


def test_config_snapshot_hash_excludes_token():
    first = RuntimeConfig(api_token="secret-one")
    second = RuntimeConfig(api_token="secret-two")
    assert first.effective_hash() == second.effective_hash()
    assert "api_token" not in first.safe_snapshot()
