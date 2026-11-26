import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from lar.core.config import RuntimeConfig
from lar.storage.database import Database, StorageError


def test_migration_is_versioned_idempotent_and_saves_safe_config(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    database = Database(path)
    assert database.migrate() is None
    assert database.migrate() is None
    assert database.health() == "ok"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"schema_migrations", "inference_requests", "benchmark_runs", "observation_outbox"} <= tables
        assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 2
    config = RuntimeConfig(api_token="do-not-store-this")
    config_hash = database.save_config(config)
    with sqlite3.connect(path) as connection:
        saved = connection.execute("SELECT config_json FROM config_snapshots").fetchone()[0]
    assert config_hash in saved or "127.0.0.1" in saved
    assert "do-not-store-this" not in saved


def test_existing_database_is_backed_up_before_migration(tmp_path):
    path = tmp_path / "existing.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE historical(id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO historical(value) VALUES('preserve')")
    backup = Database(path).migrate()
    assert backup is not None and backup.exists()
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT value FROM historical").fetchone()[0] == "preserve"
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM historical").fetchone()[0] == "preserve"


def test_legacy_results_database_path_is_refused(tmp_path):
    legacy = tmp_path / "results" / "lar.db"
    with pytest.raises(StorageError, match="protetto"):
        Database(legacy)


def test_request_lifecycle_persists_only_minimized_metadata(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    database = Database(path)
    database.migrate()
    database.record_request("req-1", stream=True, tool_count=1)
    database.update_request("req-1", status="queued")
    database.update_request("req-1", status="completed", finish_reason="tool_calls")
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT status, client_model_alias, stream, tool_count, content_persisted, completed_at, finish_reason FROM inference_requests WHERE request_id='req-1'"
        ).fetchone()
    assert row[0:5] == ("completed", "lar-auto", 1, 1, 0)
    assert row[5] is not None
    assert row[6] == "tool_calls"


def test_retention_migrations_prune_expired_rows_and_preserve_benchmarks(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    database = Database(path)
    database.migrate()
    config = RuntimeConfig()
    now = datetime.now(UTC)
    database.record_request("old", stream=False, tool_count=0)
    database.record_request("new", stream=False, tool_count=0)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE inference_requests SET created_at=? WHERE request_id='old'",
            ((now - timedelta(days=366)).isoformat(),),
        )
        connection.execute(
            "INSERT INTO observation_events(observation_id,source,observed_at,event_type,safe_payload_json,correlation_status,history_status) VALUES('evt','test',?,'seen','{}','unmatched','unknown')",
            ((now - timedelta(days=15)).isoformat(),),
        )
        connection.execute(
            "INSERT INTO observation_outbox(outbox_id,source,created_at,state,safe_payload_json) VALUES('pending','test',?,'pending','{}')",
            ((now - timedelta(days=30)).isoformat(),),
        )
    deleted = database.purge_expired(config, now=now)
    assert deleted["inference_requests"] == 1
    assert deleted["observation_events"] == 1
    assert deleted["observation_outbox"] == 0
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT request_id FROM inference_requests").fetchall() == [("new",)]
        assert connection.execute("SELECT state FROM observation_outbox").fetchone() == ("pending",)
        assert connection.execute("SELECT count(*) FROM retention_audit").fetchone() == (1,)
        assert connection.execute("SELECT max(version) FROM schema_migrations").fetchone() == (2,)


def test_controlled_legacy_benchmark_import_is_read_only_and_idempotent(tmp_path):
    import hashlib

    source = tmp_path / "results" / "lar.db"
    source.parent.mkdir()
    with sqlite3.connect(source) as connection:
        connection.executescript(
            """CREATE TABLE scans(id INTEGER PRIMARY KEY,created_at TEXT,model_count INTEGER);
               CREATE TABLE model_snapshots(id INTEGER PRIMARY KEY,scan_id INTEGER,name TEXT,digest TEXT,size_bytes INTEGER,modified_at TEXT,metadata_json TEXT,inspection_error TEXT);
               CREATE TABLE benchmark_runs(id INTEGER PRIMARY KEY,created_at TEXT,model TEXT,benchmark_id TEXT,benchmark_version INTEGER,category TEXT,status TEXT,score REAL,tests_run INTEGER,tests_passed INTEGER,metrics_json TEXT,result_json TEXT);
               INSERT INTO scans VALUES(1,'2026-01-01T00:00:00+00:00',1);
               INSERT INTO model_snapshots VALUES(1,1,'model-a','digest-a',1024,'2026-01-01T00:00:00+00:00','{}',NULL);
               INSERT INTO benchmark_runs VALUES(7,'2026-01-02T00:00:00+00:00','model-a','coding.test',1,'coding','passed',0.75,4,3,'{"tokens_per_second":12.5}','{"generated_code":"must-not-import"}');"""
        )
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    target = Database(tmp_path / "runtime.sqlite3")
    target.migrate()
    assert target.import_legacy_benchmarks(source) == {
        "source_runs": 1, "imported_runs": 1, "imported_measurements": 4, "imported_artifacts": 1
    }
    assert target.import_legacy_benchmarks(source)["imported_runs"] == 0
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    with sqlite3.connect(target.path) as connection:
        run = connection.execute(
            "SELECT status,provenance_json FROM benchmark_runs"
        ).fetchone()
        measurements = connection.execute(
            "SELECT origin,unit FROM benchmark_measurements ORDER BY metric_name"
        ).fetchall()
    assert run[0] == "passed"
    assert '"comparability": "unscored"' in run[1]
    assert len(measurements) == 4
    assert {item[0] for item in measurements} == {"legacy_unknown"}
    assert "must-not-import" not in run[1]


def test_unknown_provider_stop_survives_retention_until_operator_attests(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    database = Database(path)
    database.migrate()
    database.record_request("uncertain", stream=False, tool_count=0)
    database.update_request(
        "uncertain", status="provider_stop_unknown", error_code="provider_stop_unverified"
    )
    assert database.has_unresolved_provider_stop()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE inference_requests SET created_at='2020-01-01T00:00:00+00:00' WHERE request_id='uncertain'"
        )
    deleted = database.purge_expired(RuntimeConfig())
    assert deleted["inference_requests"] == 0
    assert database.has_unresolved_provider_stop()
    assert database.acknowledge_provider_stopped("uncertain")
    assert not database.has_unresolved_provider_stop()
    assert not database.acknowledge_provider_stopped("missing")
    with sqlite3.connect(path) as connection:
        status, error_code = connection.execute(
            "SELECT status,error_code FROM inference_requests WHERE request_id='uncertain'"
        ).fetchone()
        event = connection.execute(
            "SELECT event_type FROM model_lifecycle_events WHERE request_id='uncertain'"
        ).fetchone()
    assert (status, error_code) == ("operator_confirmed_stopped", "operator_confirmed_physical_stop")
    assert event == ("operator_confirmed_provider_stopped",)
