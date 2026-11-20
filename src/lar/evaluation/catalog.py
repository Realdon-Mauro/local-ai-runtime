import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lar.evaluation.discovery import ModelInventoryEntry


class ModelCatalog:
    """Catalogo SQLite con storico immutabile delle scansioni."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS scans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    model_count INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS model_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    digest TEXT,
                    size_bytes INTEGER NOT NULL,
                    modified_at TEXT,
                    metadata_json TEXT NOT NULL,
                    inspection_error TEXT,
                    FOREIGN KEY (scan_id) REFERENCES scans(id),
                    UNIQUE (scan_id, name)
                );

                CREATE INDEX IF NOT EXISTS idx_snapshots_scan_id
                ON model_snapshots(scan_id);

                CREATE INDEX IF NOT EXISTS idx_snapshots_digest
                ON model_snapshots(digest);
                """
            )

    def save_scan(self, models: list[ModelInventoryEntry]) -> int:
        """Salva una nuova scansione e restituisce il suo ID."""

        created_at = datetime.now(UTC).isoformat()

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO scans (created_at, model_count)
                VALUES (?, ?)
                """,
                (created_at, len(models)),
            )
            scan_id = cursor.lastrowid

            for model in models:
                metadata: dict[str, Any] = {
                    "format": model.format,
                    "family": model.family,
                    "families": model.families,
                    "parameter_size": model.parameter_size,
                    "quantization_level": model.quantization_level,
                    "capabilities": model.capabilities,
                    "model_info": model.model_info,
                }

                connection.execute(
                    """
                    INSERT INTO model_snapshots (
                        scan_id,
                        name,
                        digest,
                        size_bytes,
                        modified_at,
                        metadata_json,
                        inspection_error
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scan_id,
                        model.name,
                        model.digest,
                        model.size_bytes,
                        model.modified_at,
                        json.dumps(metadata, ensure_ascii=False),
                        model.inspection_error,
                    ),
                )

        return scan_id

    def list_scans(self) -> list[dict[str, Any]]:
        """Restituisce lo storico delle scansioni, dalla più recente."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, created_at, model_count
                FROM scans
                ORDER BY id DESC
                """
            ).fetchall()

        return [dict(row) for row in rows]

    def list_latest_models(self) -> list[dict[str, Any]]:
        """Restituisce i modelli dell'ultima scansione."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    name,
                    digest,
                    size_bytes,
                    modified_at,
                    metadata_json,
                    inspection_error
                FROM model_snapshots
                WHERE scan_id = (
                    SELECT MAX(id) FROM scans
                )
                ORDER BY name
                """
            ).fetchall()

        results = []

        for row in rows:
            model = dict(row)
            model["metadata"] = json.loads(model.pop("metadata_json"))
            results.append(model)

        return results

    def get_scan_models(self, scan_id: int) -> list[dict[str, Any]]:
        """Restituisce i modelli appartenenti a una scansione specifica."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    name,
                    digest,
                    size_bytes,
                    modified_at,
                    metadata_json,
                    inspection_error
                FROM model_snapshots
                WHERE scan_id = ?
                ORDER BY name
                """,
                (scan_id,),
            ).fetchall()

        results = []

        for row in rows:
            model = dict(row)
            model["metadata"] = json.loads(model.pop("metadata_json"))
            results.append(model)

        return results
