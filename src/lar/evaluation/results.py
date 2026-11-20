import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lar.evaluation.runner import BenchmarkResult


class BenchmarkStore:
    """Storage SQLite dello storico delle esecuzioni benchmark."""

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
                CREATE TABLE IF NOT EXISTS benchmark_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    model TEXT NOT NULL,
                    benchmark_id TEXT NOT NULL,
                    benchmark_version INTEGER NOT NULL,
                    category TEXT,
                    status TEXT NOT NULL,
                    score REAL,
                    tests_run INTEGER,
                    tests_passed INTEGER,
                    metrics_json TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_benchmark_runs_model
                ON benchmark_runs(model);

                CREATE INDEX IF NOT EXISTS idx_benchmark_runs_benchmark
                ON benchmark_runs(benchmark_id);

                CREATE INDEX IF NOT EXISTS idx_benchmark_runs_created_at
                ON benchmark_runs(created_at);
                """
            )

            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(benchmark_runs)"
                )
            }

            if "category" not in columns:
                connection.execute(
                    "ALTER TABLE benchmark_runs ADD COLUMN category TEXT"
                )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_benchmark_runs_category
                ON benchmark_runs(category)
                """
            )

    def save_result(self, result: BenchmarkResult) -> int:
        """Salva un'esecuzione e restituisce il suo ID."""

        data = result.to_dict()
        created_at = datetime.now(UTC).isoformat()

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO benchmark_runs (
                    created_at,
                    model,
                    benchmark_id,
                    benchmark_version,
                    category,
                    status,
                    score,
                    tests_run,
                    tests_passed,
                    metrics_json,
                    result_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    result.model,
                    result.benchmark_id,
                    result.benchmark_version,
                    result.category,
                    result.status,
                    result.score,
                    result.tests_run,
                    result.tests_passed,
                    json.dumps(data["metrics"], ensure_ascii=False),
                    json.dumps(data, ensure_ascii=False),
                ),
            )

            return cursor.lastrowid

    @staticmethod
    def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["metrics"] = json.loads(result.pop("metrics_json"))
        result["result"] = json.loads(result.pop("result_json"))
        return result

    def list_runs(self, limit: int | None = 100) -> list[dict[str, Any]]:
        """Restituisce le esecuzioni più recenti o tutto lo storico."""

        if limit is not None and (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
        ):
            raise ValueError("Il limite deve essere un intero positivo o None.")

        with self._connect() as connection:
            if limit is None:
                rows = connection.execute(
                    """
                    SELECT *
                    FROM benchmark_runs
                    ORDER BY id DESC
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT *
                    FROM benchmark_runs
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()

        return [self._decode_row(row) for row in rows]

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        """Restituisce una singola esecuzione tramite il suo ID."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM benchmark_runs
                WHERE id = ?
                """,
                (run_id,),
            ).fetchone()

        return self._decode_row(row) if row is not None else None
