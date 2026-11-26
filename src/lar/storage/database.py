"""Conservative, versioned SQLite storage for runtime metadata."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from uuid import uuid4

from lar.core.config import RuntimeConfig

_MIGRATION_1 = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
CREATE TABLE config_snapshots (
    config_id TEXT PRIMARY KEY, version INTEGER NOT NULL, created_at TEXT NOT NULL,
    config_json TEXT NOT NULL, source TEXT NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1))
);
CREATE UNIQUE INDEX one_active_config ON config_snapshots(active) WHERE active=1;
CREATE TABLE hardware_profiles (
    profile_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, discovered_at TEXT NOT NULL,
    discovery_version TEXT NOT NULL, profile_json TEXT NOT NULL, origins_json TEXT NOT NULL
);
CREATE TABLE resource_snapshots (
    snapshot_id TEXT PRIMARY KEY, hardware_profile_id TEXT NOT NULL REFERENCES hardware_profiles(profile_id) ON DELETE RESTRICT,
    sampled_at TEXT NOT NULL, cpu_json TEXT, ram_total_bytes INTEGER, ram_available_bytes INTEGER,
    gpu_json TEXT NOT NULL, temperatures_json TEXT NOT NULL, external_processes_json TEXT NOT NULL,
    sources_json TEXT NOT NULL, freshness TEXT NOT NULL, quality REAL
);
CREATE TABLE model_artifacts (
    artifact_id TEXT PRIMARY KEY, provider TEXT NOT NULL CHECK(provider='ollama'),
    provider_model_id TEXT NOT NULL, digest TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
    model_info_json TEXT NOT NULL, field_sources_json TEXT NOT NULL, inspection_status TEXT NOT NULL
);
CREATE TABLE model_profiles (
    profile_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES model_artifacts(artifact_id) ON DELETE RESTRICT,
    hardware_profile_id TEXT REFERENCES hardware_profiles(profile_id) ON DELETE RESTRICT,
    runtime_name TEXT, runtime_version TEXT, profile_version TEXT NOT NULL, created_at TEXT NOT NULL,
    profile_json TEXT NOT NULL, sample_count INTEGER NOT NULL CHECK(sample_count>=0), confidence REAL
);
CREATE TABLE benchmark_specs (
    benchmark_id TEXT NOT NULL, benchmark_version TEXT NOT NULL, category TEXT, spec_digest TEXT NOT NULL,
    spec_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(benchmark_id, benchmark_version)
);
CREATE TABLE benchmark_runs (
    run_id TEXT PRIMARY KEY, benchmark_id TEXT NOT NULL, benchmark_version TEXT NOT NULL,
    artifact_id TEXT REFERENCES model_artifacts(artifact_id) ON DELETE RESTRICT,
    hardware_profile_id TEXT REFERENCES hardware_profiles(profile_id) ON DELETE RESTRICT,
    started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL, runner_version TEXT,
    runtime_version TEXT, context_tokens INTEGER, generation_options_json TEXT, environment_json TEXT,
    error_code TEXT, provenance_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(benchmark_id, benchmark_version) REFERENCES benchmark_specs(benchmark_id,benchmark_version) ON DELETE RESTRICT
);
CREATE TABLE benchmark_measurements (
    measurement_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES benchmark_runs(run_id) ON DELETE CASCADE,
    metric_name TEXT NOT NULL, value REAL, unit TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('minimize','maximize','target','none')),
    origin TEXT NOT NULL CHECK(origin IN ('measured','model_declared','estimated','legacy_unknown')),
    confidence REAL, sample_count INTEGER NOT NULL CHECK(sample_count>=0)
);
CREATE TABLE inference_requests (
    request_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, completed_at TEXT, client_model_alias TEXT NOT NULL,
    selected_artifact TEXT REFERENCES model_artifacts(artifact_id) ON DELETE RESTRICT,
    status TEXT NOT NULL, finish_reason TEXT, error_code TEXT, stream INTEGER NOT NULL CHECK(stream IN (0,1)),
    tool_count INTEGER NOT NULL DEFAULT 0, content_persisted INTEGER NOT NULL DEFAULT 0 CHECK(content_persisted IN (0,1))
);
CREATE TABLE routing_decisions (
    decision_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE REFERENCES inference_requests(request_id) ON DELETE CASCADE,
    hardware_profile_id TEXT REFERENCES hardware_profiles(profile_id) ON DELETE RESTRICT,
    snapshot_id TEXT REFERENCES resource_snapshots(snapshot_id) ON DELETE RESTRICT,
    config_id TEXT REFERENCES config_snapshots(config_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL, router_version TEXT NOT NULL, policy_version TEXT NOT NULL,
    analyzer_version TEXT NOT NULL, status TEXT NOT NULL, reason_codes_json TEXT NOT NULL, task_profile_json TEXT
);
CREATE TABLE routing_candidates (
    decision_id TEXT NOT NULL REFERENCES routing_decisions(decision_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL, artifact_id TEXT REFERENCES model_artifacts(artifact_id) ON DELETE RESTRICT,
    profile_id TEXT REFERENCES model_profiles(profile_id) ON DELETE RESTRICT, estimate_json TEXT,
    eligible INTEGER NOT NULL CHECK(eligible IN (0,1)), status TEXT NOT NULL, reasons_json TEXT NOT NULL,
    pareto_front INTEGER, evidence_coverage REAL NOT NULL, PRIMARY KEY(decision_id,ordinal)
);
CREATE TABLE request_metrics (
    request_id TEXT PRIMARY KEY REFERENCES inference_requests(request_id) ON DELETE CASCADE,
    queue_wait_ms REAL, model_load_ms REAL, first_token_ms REAL, total_ms REAL,
    prompt_tokens INTEGER, output_tokens INTEGER, generation_tps REAL, cpu_peak_percent REAL,
    ram_peak_bytes INTEGER, vram_peak_bytes INTEGER, temperature_peak_c REAL, sources_json TEXT NOT NULL
);
CREATE TABLE model_lifecycle_events (
    event_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL,
    artifact_id TEXT REFERENCES model_artifacts(artifact_id) ON DELETE RESTRICT,
    request_id TEXT REFERENCES inference_requests(request_id) ON DELETE SET NULL,
    event_type TEXT NOT NULL, ownership_before TEXT, ownership_after TEXT, state_before TEXT,
    state_after TEXT, duration_ms REAL, error_code TEXT, details_json TEXT NOT NULL
);
CREATE TABLE observation_events (
    observation_id TEXT PRIMARY KEY, source TEXT NOT NULL, native_id TEXT, session_id TEXT,
    workflow_id TEXT, observed_at TEXT NOT NULL, event_type TEXT NOT NULL,
    safe_payload_json TEXT NOT NULL, correlation_status TEXT NOT NULL, history_status TEXT NOT NULL
);
CREATE UNIQUE INDEX observation_native_identity ON observation_events(source,native_id) WHERE native_id IS NOT NULL;
CREATE TABLE observation_outbox (
    outbox_id TEXT PRIMARY KEY, source TEXT NOT NULL, native_id TEXT, sequence_no INTEGER,
    created_at TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT, safe_payload_json TEXT NOT NULL, last_error_code TEXT
);
CREATE INDEX resource_snapshot_time ON resource_snapshots(hardware_profile_id,sampled_at DESC);
CREATE INDEX artifact_seen ON model_artifacts(provider_model_id,last_seen DESC);
CREATE INDEX benchmark_run_time ON benchmark_runs(artifact_id,started_at DESC);
CREATE INDEX inference_status_time ON inference_requests(status,created_at DESC);
CREATE INDEX lifecycle_request_time ON model_lifecycle_events(request_id,occurred_at);
"""
_MIGRATION_2 = """
CREATE TABLE retention_audit (
    audit_id TEXT PRIMARY KEY,
    executed_at TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    deleted_json TEXT NOT NULL
);
CREATE INDEX retention_audit_time ON retention_audit(executed_at DESC);
"""
_MIGRATIONS = ((1, "initial_runtime_schema", _MIGRATION_1), (2, "retention_audit", _MIGRATION_2))


class StorageError(RuntimeError):
    """Database integrity or migration failure."""


class Database:
    """SQLite database; never imports or mutates legacy ``results/lar.db``."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser().resolve()
        if self.path.name == "lar.db" and self.path.parent.name == "results":
            raise StorageError("Il DB legacy results/lar.db è protetto e non viene aperto dal runtime.")

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def migrate(self) -> Path | None:
        """Back up an existing DB, apply checksum-tracked additive schema, verify."""
        if self.path.exists() and self._is_current():
            if self.health() != "ok":
                raise StorageError("Database runtime non integro.")
            return None
        existed = self.path.exists() and self.path.stat().st_size > 0
        backup = self._backup() if existed else None
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
                ).fetchone()
                applied_versions: dict[int, str] = {}
                if exists:
                    applied_versions = {
                        int(row["version"]): row["checksum"]
                        for row in connection.execute(
                            "SELECT version,checksum FROM schema_migrations"
                        )
                    }
                    if max(applied_versions, default=0) > len(_MIGRATIONS):
                        raise StorageError("Database runtime creato da una versione più recente.")
                for version, name, sql in _MIGRATIONS:
                    checksum = hashlib.sha256(sql.encode()).hexdigest()
                    if version in applied_versions:
                        if applied_versions[version] != checksum:
                            raise StorageError(f"Checksum migratione {version} non valido.")
                        continue
                    for statement in sql.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(?,?,?,?)",
                        (version, name, checksum, datetime.now(UTC).isoformat()),
                    )
                connection.execute(f"PRAGMA user_version={len(_MIGRATIONS)}")
                result = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise StorageError(f"SQLite integrity_check fallito: {result}")
                fk_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
                if fk_errors:
                    raise StorageError("SQLite foreign_key_check ha rilevato errori.")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return backup

    def _is_current(self) -> bool:
        try:
            with sqlite3.connect(self.path) as connection:
                rows = connection.execute(
                    "SELECT version,checksum FROM schema_migrations"
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return False
            raise StorageError("Impossibile leggere il registro migration SQLite.") from exc
        applied = {int(version): checksum for version, checksum in rows}
        if not applied:
            return False
        if max(applied) > len(_MIGRATIONS):
            raise StorageError("Database runtime creato da una versione più recente.")
        for version, _name, sql in _MIGRATIONS:
            if version in applied and applied[version] != hashlib.sha256(sql.encode()).hexdigest():
                raise StorageError(f"Checksum migratione {version} non valido.")
        return len(applied) == len(_MIGRATIONS)

    def _backup(self) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = self.path.with_name(f"{self.path.name}.pre-migration-{stamp}.sqlite3")
        with sqlite3.connect(self.path) as source, sqlite3.connect(backup_path) as target:
            source.backup(target)
            check = target.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                backup_path.unlink(missing_ok=True)
                raise StorageError(f"Backup SQLite non integro: {check}")
        os.chmod(backup_path, 0o600)
        return backup_path

    def health(self) -> str:
        if not self.path.exists():
            raise StorageError("Database runtime non inizializzato.")
        uri = f"{self.path.as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            result = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            if result == "ok" and connection.execute("PRAGMA foreign_key_check").fetchall():
                return "foreign_key_error"
            return result

    def backup(self) -> Path:
        """Create and verify a point-in-time SQLite backup on explicit request."""
        if not self.path.exists():
            raise StorageError("Database runtime non inizializzato.")
        return self._backup()

    def save_config(self, config: RuntimeConfig, *, source: str = "runtime") -> str:
        """Store a non-secret config snapshot and return its effective hash."""
        config_hash = config.effective_hash()
        identifier = config_hash
        payload = json.dumps(config.safe_snapshot(), sort_keys=True, separators=(",", ":"))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE config_snapshots SET active=0 WHERE active=1")
            connection.execute(
                "INSERT OR IGNORE INTO config_snapshots(config_id,version,created_at,config_json,source,active) VALUES(?,?,?,?,?,0)",
                (identifier, config.schema_version, datetime.now(UTC).isoformat(), payload, source),
            )
            connection.execute("UPDATE config_snapshots SET active=1 WHERE config_id=?", (identifier,))
        return config_hash

    def save_hardware_profile(self, profile: object) -> str:
        profile_id = str(profile.fingerprint)
        payload = self._json_value(profile)
        origins = {"host_profile": getattr(profile, "source", "unavailable")}
        with self.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO hardware_profiles
                   (profile_id,fingerprint,discovered_at,discovery_version,profile_json,origins_json)
                   VALUES(?,?,?,?,?,?)""",
                (profile_id, profile_id, profile.discovered_at.isoformat(),
                 "resource-v1", json.dumps(payload, sort_keys=True), json.dumps(origins)),
            )
        return profile_id

    def save_model_artifact(
        self,
        *,
        model_id: str,
        digest: str | None,
        model_info: dict[str, object],
        inspection_status: str = "available",
    ) -> str:
        artifact_id = hashlib.sha256(f"ollama\0{model_id}\0{digest or ''}".encode()).hexdigest()
        timestamp = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO model_artifacts
                   (artifact_id,provider,provider_model_id,digest,first_seen,last_seen,
                    model_info_json,field_sources_json,inspection_status)
                   VALUES(?,'ollama',?,?,?,?,?,?,?)
                   ON CONFLICT(artifact_id) DO UPDATE SET last_seen=excluded.last_seen,
                    model_info_json=excluded.model_info_json,
                    field_sources_json=excluded.field_sources_json,
                    inspection_status=excluded.inspection_status""",
                (artifact_id, model_id, digest, timestamp, timestamp,
                 json.dumps(model_info, sort_keys=True, default=str),
                 json.dumps({key: "ollama:show" for key in model_info}), inspection_status),
            )
        return artifact_id

    def save_resource_snapshot(self, snapshot: object, hardware_profile_id: str) -> None:
        data = self._json_value(snapshot)
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO resource_snapshots
                   (snapshot_id,hardware_profile_id,sampled_at,cpu_json,ram_total_bytes,
                    ram_available_bytes,gpu_json,temperatures_json,external_processes_json,
                    sources_json,freshness,quality)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data["snapshot_id"], hardware_profile_id, data["sampled_at"],
                 json.dumps({"utilization_percent": data["cpu_utilization_percent"]}),
                 data["ram_total_bytes"], data["ram_available_bytes"],
                 json.dumps(data["gpus"], sort_keys=True),
                 json.dumps(data["temperatures"], sort_keys=True),
                 json.dumps(data["external_processes"], sort_keys=True),
                 json.dumps(data["sources"], sort_keys=True), "fresh", None),
            )

    def save_routing_decision(
        self,
        decision: object,
        *,
        request_id: str,
        hardware_profile_id: str,
        config_id: str,
        artifact_ids: dict[str, str],
    ) -> None:
        created_at = datetime.now(UTC).isoformat()
        task = decision.task
        task_payload = self._json_value(task)
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO routing_decisions
                   (decision_id,request_id,hardware_profile_id,snapshot_id,config_id,created_at,
                    router_version,policy_version,analyzer_version,status,reason_codes_json,task_profile_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (decision.decision_id, request_id, hardware_profile_id, decision.snapshot_id,
                 config_id, created_at, "model-router-v1", decision.policy_version,
                 task.analyzer_version, decision.status, json.dumps(decision.reasons),
                 json.dumps(task_payload, sort_keys=True)),
            )
            for ordinal, trace in enumerate(decision.candidate_traces):
                estimate = self._json_value(trace.estimate) if trace.estimate is not None else None
                connection.execute(
                    """INSERT INTO routing_candidates
                       (decision_id,ordinal,artifact_id,estimate_json,eligible,status,reasons_json,
                        pareto_front,evidence_coverage)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (decision.decision_id, ordinal, artifact_ids.get(trace.model_id),
                     json.dumps(estimate, sort_keys=True) if estimate is not None else None,
                     int(trace.status.value in {"eligible", "unscored", "constrained"}),
                     trace.status.value, json.dumps(trace.reasons), trace.pareto_front,
                     trace.evidence_coverage),
                )
            selected_artifact = artifact_ids.get(decision.selected_model_id or "")
            connection.execute(
                "UPDATE inference_requests SET selected_artifact=? WHERE request_id=?",
                (selected_artifact, request_id),
            )

    def record_model_fallback(
        self, request_id: str, *, from_model: str, to_model: str, artifact_id: str
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE inference_requests SET selected_artifact=? WHERE request_id=?",
                (artifact_id, request_id),
            )
            connection.execute(
                """INSERT INTO model_lifecycle_events
                   (event_id,occurred_at,request_id,event_type,details_json)
                   VALUES(?,?,?,'pre_inference_model_fallback',?)""",
                (str(uuid4()), datetime.now(UTC).isoformat(), request_id,
                 json.dumps({"from_model": from_model, "to_model": to_model,
                             "reason": "primary_load_http_404"}, sort_keys=True)),
            )

    def append_opencode_observation(self, observation: object) -> tuple[str, str, bool]:
        """Append an allowlisted event; deduplicate only by native source identity."""
        data = observation.model_dump(mode="json")
        source = data["source"]
        native_id = data["native_id"]
        observation_id = str(uuid4())
        event_type = data["event_type"]
        workflow_id = str(uuid4()) if event_type == "user_message_received" else None
        safe_payload = {
            key: data[key]
            for key in (
                "status", "tool_category", "model", "duration_ms", "input_tokens",
                "output_tokens",
            )
            if data[key] is not None
        }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if native_id is not None:
                existing = connection.execute(
                    "SELECT observation_id,workflow_id FROM observation_events WHERE source=? AND native_id=?",
                    (source, native_id),
                ).fetchone()
                if existing:
                    return str(existing["observation_id"]), existing["workflow_id"], True
            if data.get("workflow_id"):
                correlated = connection.execute(
                    "SELECT 1 FROM observation_events WHERE workflow_id=? AND event_type='user_message_received'",
                    (data["workflow_id"],),
                ).fetchone()
                if not correlated:
                    raise StorageError("workflow_id privo di un evento utente LAR correlabile.")
                workflow_id = data["workflow_id"]
            connection.execute(
                """INSERT INTO observation_events
                   (observation_id,source,native_id,session_id,workflow_id,observed_at,event_type,
                    safe_payload_json,correlation_status,history_status)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (observation_id, source, native_id, data["session_id"], workflow_id,
                 data["observed_at"], event_type, json.dumps(safe_payload, sort_keys=True),
                 "unmatched" if workflow_id is None else "native", data["history_status"]),
            )
        return observation_id, workflow_id, False

    def save_request_metrics(self, request_id: str, metrics: dict[str, object]) -> None:
        sources = metrics.pop("sources", {})
        columns = (
            "queue_wait_ms", "model_load_ms", "first_token_ms", "total_ms",
            "prompt_tokens", "output_tokens", "generation_tps", "cpu_peak_percent",
            "ram_peak_bytes", "vram_peak_bytes", "temperature_peak_c",
        )
        values = [metrics.get(column) for column in columns]
        placeholders = ",".join("?" for _ in range(len(columns) + 2))
        assignments = ",".join(f"{column}=excluded.{column}" for column in columns)
        with self.connect() as connection:
            connection.execute(
                f"""INSERT INTO request_metrics(request_id,{','.join(columns)},sources_json)
                    VALUES({placeholders}) ON CONFLICT(request_id) DO UPDATE SET
                    {assignments},sources_json=excluded.sources_json""",
                [request_id, *values, json.dumps(sources, sort_keys=True)],
            )

    @classmethod
    def _json_value(cls, value: object) -> object:
        if is_dataclass(value):
            return cls._json_value(asdict(value))
        if isinstance(value, dict):
            return {key: cls._json_value(item) for key, item in value.items()}
        if isinstance(value, (set, frozenset)):
            return sorted(cls._json_value(item) for item in value)
        if isinstance(value, (tuple, list)):
            return [cls._json_value(item) for item in value]
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, datetime):
            return value.isoformat()
        return value

    def record_request(
        self,
        request_id: str,
        *,
        stream: bool,
        tool_count: int,
        status: str = "received",
    ) -> None:
        """Persist metadata only; prompt, output and tool arguments are excluded."""
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO inference_requests
                   (request_id, created_at, client_model_alias, status, stream, tool_count)
                   VALUES (?, ?, 'lar-auto', ?, ?, ?)""",
                (request_id, datetime.now(UTC).isoformat(), status, int(stream), tool_count),
            )

    def update_request(
        self,
        request_id: str,
        *,
        status: str,
        error_code: str | None = None,
        finish_reason: str | None = None,
    ) -> None:
        """Update request lifecycle state, marking terminal transitions with a timestamp."""
        terminal = {
            "completed", "failed", "cancelled", "queue_timeout", "request_timeout",
            "rejected", "blocked", "provider_stop_unknown",
        }
        completed_at = datetime.now(UTC).isoformat() if status in terminal else None
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE inference_requests SET status=?,
                   completed_at=COALESCE(?, completed_at),
                   error_code=COALESCE(?, error_code),
                   finish_reason=COALESCE(?, finish_reason)
                   WHERE request_id=?""",
                (status, completed_at, error_code, finish_reason, request_id),
            )
            if cursor.rowcount != 1:
                raise StorageError(f"Richiesta runtime sconosciuta: {request_id}")

    def purge_expired(self, config: RuntimeConfig, *, now: datetime | None = None) -> dict[str, int]:
        """Apply ADR-004 retention in one transaction and append a deletion summary."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        timestamp = current.isoformat()
        resource_cutoff = (current - timedelta(days=config.retention_resource_days)).isoformat()
        metrics_cutoff = (current - timedelta(days=config.retention_inference_metrics_days)).isoformat()
        events_cutoff = (current - timedelta(days=config.retention_events_days)).isoformat()
        deleted: dict[str, int] = {}
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            old_snapshots = [row[0] for row in connection.execute(
                "SELECT snapshot_id FROM resource_snapshots WHERE sampled_at < ?", (resource_cutoff,)
            )]
            if old_snapshots:
                connection.executemany(
                    "UPDATE routing_decisions SET snapshot_id=NULL WHERE snapshot_id=?",
                    ((snapshot_id,) for snapshot_id in old_snapshots),
                )
            for table, column, cutoff in (
                ("resource_snapshots", "sampled_at", resource_cutoff),
                ("request_metrics", "request_id", None),
                ("model_lifecycle_events", "occurred_at", events_cutoff),
                ("observation_events", "observed_at", events_cutoff),
            ):
                if table == "request_metrics":
                    cursor = connection.execute(
                        "DELETE FROM request_metrics WHERE request_id IN "
                        "(SELECT request_id FROM inference_requests WHERE created_at < ? "
                        "AND NOT (status IN ('provider_stop_unknown','blocked') "
                        "AND error_code IN ('provider_stop_unknown','provider_stop_unverified')))",
                        (metrics_cutoff,),
                    )
                else:
                    cursor = connection.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
                deleted[table] = cursor.rowcount
            cursor = connection.execute(
                "DELETE FROM inference_requests WHERE created_at < ? "
                "AND NOT (status IN ('provider_stop_unknown','blocked') "
                "AND error_code IN ('provider_stop_unknown','provider_stop_unverified'))",
                (metrics_cutoff,),
            )
            deleted["inference_requests"] = cursor.rowcount
            cursor = connection.execute(
                "DELETE FROM observation_outbox WHERE state IN ('delivered','acked') AND created_at < ?",
                (events_cutoff,),
            )
            deleted["observation_outbox"] = cursor.rowcount
            connection.execute(
                "INSERT INTO retention_audit(audit_id,executed_at,policy_version,deleted_json) VALUES(?,?,?,?)",
                (str(uuid4()), timestamp, config.effective_hash(), json.dumps(deleted, sort_keys=True)),
            )
        return deleted

    def has_unresolved_provider_stop(self) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT 1 FROM inference_requests WHERE
                   (status='provider_stop_unknown' AND error_code='provider_stop_unverified') OR
                   (status='blocked' AND error_code='provider_stop_unknown') LIMIT 1"""
            ).fetchone()
        return row is not None

    def acknowledge_provider_stopped(self, request_id: str) -> bool:
        """Record an explicit operator attestation; this does not probe Ollama."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT 1 FROM inference_requests WHERE request_id=? AND
                   ((status='provider_stop_unknown' AND error_code='provider_stop_unverified') OR
                    (status='blocked' AND error_code='provider_stop_unknown'))""",
                (request_id,),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE inference_requests SET status='operator_confirmed_stopped', "
                "error_code='operator_confirmed_physical_stop' WHERE request_id=?",
                (request_id,),
            )
            connection.execute(
                """INSERT INTO model_lifecycle_events
                   (event_id,occurred_at,request_id,event_type,details_json)
                   VALUES(?,?,?,'operator_confirmed_provider_stopped',?)""",
                (str(uuid4()), datetime.now(UTC).isoformat(), request_id,
                 json.dumps({"confirmation": "explicit_operator_attestation"})),
            )
        return True

    def import_legacy_benchmarks(self, source_path: Path) -> dict[str, int]:
        """Read legacy catalog/runs into this DB without writing to the source."""
        source = Path(source_path).expanduser().resolve()
        if source == self.path:
            raise StorageError("Il database legacy deve essere distinto dal database runtime.")
        if not source.is_file():
            raise StorageError("Database legacy non trovato.")
        if not self.path.exists() or not self._is_current():
            raise StorageError("Inizializzare prima il database runtime con lar db migrate.")
        self.backup()
        source_id = hashlib.sha256(str(source).encode()).hexdigest()[:16]
        source_uri = f"{source.as_uri()}?mode=ro"
        try:
            legacy = sqlite3.connect(source_uri, uri=True)
            legacy.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            raise StorageError("Impossibile aprire il database legacy in sola lettura.") from exc
        try:
            tables = {
                row[0]
                for row in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "benchmark_runs" not in tables:
                raise StorageError("Schema legacy senza tabella benchmark_runs.")
            run_columns = {row[1] for row in legacy.execute("PRAGMA table_info(benchmark_runs)")}
            required = {
                "id", "created_at", "model", "benchmark_id", "benchmark_version", "category",
                "status", "score", "tests_run", "tests_passed", "metrics_json",
            }
            if not required <= run_columns:
                raise StorageError("Schema benchmark legacy non compatibile con l’importazione.")
            runs = legacy.execute("SELECT * FROM benchmark_runs ORDER BY id").fetchall()
            snapshots: list[sqlite3.Row] = []
            if "model_snapshots" in tables:
                snapshots = legacy.execute("SELECT * FROM model_snapshots ORDER BY id").fetchall()
            scan_times: dict[int, str] = {}
            if "scans" in tables:
                scan_times = {
                    int(row["id"]): str(row["created_at"])
                    for row in legacy.execute("SELECT id,created_at FROM scans")
                }
        except sqlite3.Error as exc:
            raise StorageError("Errore di lettura nello schema benchmark legacy.") from exc
        finally:
            legacy.close()

        artifact_ids_by_model: dict[str, str] = {}
        artifact_records: dict[str, tuple[str, str | None, str, str, str]] = {}
        for snapshot in snapshots:
            keys = set(snapshot.keys())
            name = str(snapshot["name"])
            digest = snapshot["digest"] if "digest" in keys else None
            artifact_id = hashlib.sha256(f"ollama\0{name}\0{digest or ''}".encode()).hexdigest()
            scan_id = snapshot["scan_id"] if "scan_id" in keys else None
            seen = scan_times.get(int(scan_id), str(snapshot["modified_at"] or "unknown")) if scan_id is not None else "unknown"
            metadata = snapshot["metadata_json"] if "metadata_json" in keys else "{}"
            artifact_records[artifact_id] = (name, digest, seen, str(metadata or "{}"), str(snapshot["inspection_error"] or "") if "inspection_error" in keys else "")
            artifact_ids_by_model[name] = artifact_id

        inserted_runs = inserted_measurements = inserted_artifacts = 0
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for artifact_id, (name, digest, seen, metadata, inspection_error) in artifact_records.items():
                    connection.execute(
                        """INSERT OR IGNORE INTO model_artifacts
                           (artifact_id,provider,provider_model_id,digest,first_seen,last_seen,
                            model_info_json,field_sources_json,inspection_status)
                           VALUES(?,'ollama',?,?,?,?,?,?,?)""",
                        (artifact_id, name, digest, seen, seen, metadata,
                         json.dumps({"origin": "legacy_unknown"}),
                         "error" if inspection_error else "legacy_unknown"),
                    )
                    inserted_artifacts += connection.execute("SELECT changes()").fetchone()[0]
                specs: set[tuple[str, str, str | None]] = set()
                for run in runs:
                    benchmark_id = str(run["benchmark_id"])
                    benchmark_version = str(run["benchmark_version"])
                    category = run["category"]
                    specs.add((benchmark_id, benchmark_version, category))
                for benchmark_id, version, category in specs:
                    spec_payload = {"origin": "legacy_unknown", "spec_available": False}
                    spec_digest = hashlib.sha256(
                        f"legacy-unknown\0{benchmark_id}\0{version}".encode()
                    ).hexdigest()
                    connection.execute(
                        """INSERT OR IGNORE INTO benchmark_specs
                           (benchmark_id,benchmark_version,category,spec_digest,spec_json,created_at)
                           VALUES(?,?,?,?,?,?)""",
                        (benchmark_id, version, category, spec_digest,
                         json.dumps(spec_payload, sort_keys=True), datetime.now(UTC).isoformat()),
                    )
                for run in runs:
                    original_id = str(run["id"])
                    run_id = f"legacy:{source_id}:{original_id}"
                    model = str(run["model"])
                    artifact_id = artifact_ids_by_model.get(model)
                    created_at = str(run["created_at"])
                    provenance = {
                        "origin": "legacy_import",
                        "source_id": source_id,
                        "original_run_id": original_id,
                        "comparability": "unscored",
                        "reason": "legacy_hardware_runtime_options_or_spec_unavailable",
                    }
                    cursor = connection.execute(
                        """INSERT OR IGNORE INTO benchmark_runs
                           (run_id,benchmark_id,benchmark_version,artifact_id,started_at,status,
                            provenance_json)
                           VALUES(?,?,?,?,?,?,?)""",
                        (run_id, str(run["benchmark_id"]), str(run["benchmark_version"]),
                         artifact_id, created_at, str(run["status"]),
                         json.dumps(provenance, sort_keys=True)),
                    )
                    if cursor.rowcount == 0:
                        continue
                    inserted_runs += 1
                    measurements: list[tuple[str, float | None, str]] = [
                        ("score", run["score"], "score"),
                        ("tests_run", run["tests_run"], "count"),
                        ("tests_passed", run["tests_passed"], "count"),
                    ]
                    metrics = json.loads(run["metrics_json"] or "{}")
                    if not isinstance(metrics, dict):
                        raise StorageError(f"Metriche legacy non valide nel run {original_id}.")
                    for metric_name, value in metrics.items():
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            measurements.append((str(metric_name), float(value), "legacy_unit_unknown"))
                    for metric_name, value, unit in measurements:
                        measurement_id = hashlib.sha256(f"{run_id}\0{metric_name}".encode()).hexdigest()
                        numeric_value = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
                        connection.execute(
                            """INSERT INTO benchmark_measurements
                               (measurement_id,run_id,metric_name,value,unit,direction,origin,
                                confidence,sample_count)
                               VALUES(?,?,?,?,?,'none','legacy_unknown',NULL,1)""",
                            (measurement_id, run_id, metric_name, numeric_value, unit),
                        )
                        inserted_measurements += 1
            except Exception:
                connection.rollback()
                raise
        return {
            "source_runs": len(runs),
            "imported_runs": inserted_runs,
            "imported_measurements": inserted_measurements,
            "imported_artifacts": inserted_artifacts,
        }
