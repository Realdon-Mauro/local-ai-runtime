import pytest

from lar.evaluation.metrics import GenerationMetrics
from lar.evaluation.results import BenchmarkStore
from lar.evaluation.runner import BenchmarkResult


def make_result(
    model="test-model:latest",
    benchmark_id="coding.file_stats",
    score=1.0,
):
    return BenchmarkResult(
        model=model,
        benchmark_id=benchmark_id,
        benchmark_version=1,
        category="coding",
        status="passed",
        score=score,
        tests_run=10,
        tests_passed=10,
        generated_code="def analyze_files(files): return {}",
        generation_response={"response": "def analyze_files(files): return {}"},
        metrics=GenerationMetrics(
            total_duration_ns=2_000_000_000,
            load_duration_ns=500_000_000,
            prompt_eval_count=100,
            prompt_eval_duration_ns=1_000_000_000,
            eval_count=100,
            eval_duration_ns=2_000_000_000,
        ),
        sandbox_result=None,
    )


def test_save_and_get_result(tmp_path):
    store = BenchmarkStore(tmp_path / "results.db")
    run_id = store.save_result(make_result())

    stored = store.get_run(run_id)

    assert stored is not None
    assert stored["model"] == "test-model:latest"
    assert stored["benchmark_id"] == "coding.file_stats"
    assert stored["status"] == "passed"
    assert stored["score"] == 1.0
    assert stored["metrics"]["eval_count"] == 100
    assert stored["result"]["generated_code"] == (
        "def analyze_files(files): return {}"
    )


def test_run_history_is_ordered_newest_first(tmp_path):
    store = BenchmarkStore(tmp_path / "results.db")

    first_id = store.save_result(make_result(model="model-one"))
    second_id = store.save_result(make_result(model="model-two"))

    runs = store.list_runs()

    assert len(runs) == 2
    assert runs[0]["id"] == second_id
    assert runs[1]["id"] == first_id


def test_list_runs_respects_limit(tmp_path):
    store = BenchmarkStore(tmp_path / "results.db")

    for index in range(3):
        store.save_result(make_result(model=f"model-{index}"))

    runs = store.list_runs(limit=2)

    assert len(runs) == 2


def test_get_unknown_run_returns_none(tmp_path):
    store = BenchmarkStore(tmp_path / "results.db")

    assert store.get_run(999) is None


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "10"])
def test_list_runs_rejects_invalid_limit(tmp_path, limit):
    store = BenchmarkStore(tmp_path / "results.db")

    with pytest.raises(ValueError):
        store.list_runs(limit=limit)


def test_benchmark_store_coexists_with_model_catalog(tmp_path):
    from lar.evaluation.catalog import ModelCatalog
    from lar.evaluation.discovery import ModelInventoryEntry

    database = tmp_path / "lar.db"

    catalog = ModelCatalog(database)
    scan_id = catalog.save_scan([
        ModelInventoryEntry(
            name="test-model:latest",
            digest="abc123",
            size_bytes=1024,
            modified_at="2026-11-16T10:00:00Z",
            format="gguf",
            family="test",
            parameter_size="7B",
            quantization_level="Q4_K_M",
            capabilities=["completion"],
            model_info={},
        )
    ])

    store = BenchmarkStore(database)
    run_id = store.save_result(make_result())

    assert catalog.list_scans()[0]["id"] == scan_id
    assert catalog.list_latest_models()[0]["name"] == "test-model:latest"
    assert store.get_run(run_id)["status"] == "passed"


def test_migrates_legacy_database_without_losing_runs(tmp_path):
    import sqlite3

    database = tmp_path / "legacy.db"

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE benchmark_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                model TEXT NOT NULL,
                benchmark_id TEXT NOT NULL,
                benchmark_version INTEGER NOT NULL,
                status TEXT NOT NULL,
                score REAL,
                tests_run INTEGER,
                tests_passed INTEGER,
                metrics_json TEXT NOT NULL,
                result_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO benchmark_runs (
                created_at, model, benchmark_id, benchmark_version,
                status, score, tests_run, tests_passed,
                metrics_json, result_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-01-01T10:00:00+00:00",
                "legacy-model",
                "coding.file_stats",
                1,
                "passed",
                0.8,
                10,
                8,
                "{}",
                '{"legacy": true}',
            ),
        )

    store = BenchmarkStore(database)

    old_run = store.get_run(1)
    assert old_run is not None
    assert old_run["model"] == "legacy-model"
    assert old_run["category"] is None
    assert old_run["result"] == {"legacy": True}

    new_run_id = store.save_result(make_result())
    new_run = store.get_run(new_run_id)

    assert new_run is not None
    assert new_run["category"] == "coding"


def test_list_runs_without_limit_returns_complete_history(tmp_path):
    store = BenchmarkStore(tmp_path / "results.db")

    for index in range(5):
        store.save_result(make_result(model=f"model-{index}"))

    runs = store.list_runs(limit=None)

    assert len(runs) == 5
    assert runs[0]["model"] == "model-4"
    assert runs[-1]["model"] == "model-0"
