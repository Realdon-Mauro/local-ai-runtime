from pathlib import Path
from types import SimpleNamespace

import pytest

from lar import cli


def test_parser_benchmark_run():
    args = cli.build_parser().parse_args(
        [
            "benchmark",
            "run",
            "coding.file_stats",
            "--model",
            "qwen2.5-coder:7b",
            "--database",
            "test.db",
            "--benchmarks-dir",
            "specs",
        ]
    )

    assert args.command == "benchmark"
    assert args.benchmark_command == "run"
    assert args.benchmark_id == "coding.file_stats"
    assert args.model == "qwen2.5-coder:7b"
    assert args.database == Path("test.db")
    assert args.benchmarks_dir == Path("specs")


def test_parser_scan_and_history():
    parser = cli.build_parser()

    scan_args = parser.parse_args(["scan", "--show"])
    history_args = parser.parse_args(["history"])

    assert scan_args.command == "scan"
    assert scan_args.show is True
    assert history_args.command == "history"


@pytest.mark.asyncio
async def test_run_benchmark_rejects_missing_model(monkeypatch, tmp_path, capsys):
    class FakeProvider:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def health_check(self):
            return {"version": "test"}

        async def list_models(self):
            return [{"name": "installed-model"}]

    monkeypatch.setattr(cli, "OllamaProvider", FakeProvider)
    monkeypatch.setattr(
        cli,
        "find_benchmark",
        lambda *_: SimpleNamespace(id="coding.file_stats", version=1),
    )

    result = await cli.run_benchmark(
        "coding.file_stats",
        "missing-model",
        tmp_path / "test.db",
        tmp_path,
    )

    captured = capsys.readouterr()

    assert result == 1
    assert "missing-model" in captured.err
    assert "installed-model" in captured.err


@pytest.mark.asyncio
async def test_run_benchmark_prints_returned_run_id(monkeypatch, tmp_path, capsys):
    benchmark = SimpleNamespace(id="coding.file_stats", version=1)
    fake_result = SimpleNamespace(
        status="passed",
        score=1.0,
        tests_passed=10,
        tests_run=10,
        model="installed-model",
        metrics=SimpleNamespace(generation_tokens_per_second=None),
        error=None,
    )

    class FakeProvider:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def health_check(self):
            return {"version": "test"}

        async def list_models(self):
            return [{"name": "installed-model"}]

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            pass

        async def run_with_id(self, model, spec):
            assert model == "installed-model"
            assert spec is benchmark
            return fake_result, 42

    monkeypatch.setattr(cli, "OllamaProvider", FakeProvider)
    monkeypatch.setattr(cli, "find_benchmark", lambda *_: benchmark)
    monkeypatch.setattr(cli, "ModelManager", lambda provider: object())
    monkeypatch.setattr(cli, "BenchmarkRunner", lambda provider: object())
    monkeypatch.setattr(cli, "BenchmarkStore", lambda path: object())
    monkeypatch.setattr(cli, "BenchmarkOrchestrator", FakeOrchestrator)

    result = await cli.run_benchmark(
        "coding.file_stats",
        "installed-model",
        tmp_path / "test.db",
        tmp_path,
    )

    captured = capsys.readouterr()

    assert result == 0
    assert "ID esecuzione: 42" in captured.out
    assert "Stato: passed" in captured.out


def test_parser_benchmark_all():
    args = cli.build_parser().parse_args(
        [
            "benchmark",
            "all",
            "--category",
            "coding",
            "--model",
            "qwen2.5-coder:7b",
            "--database",
            "test.db",
            "--benchmarks-dir",
            "specs",
        ]
    )

    assert args.command == "benchmark"
    assert args.benchmark_command == "all"
    assert args.category == "coding"
    assert args.model == "qwen2.5-coder:7b"
    assert args.database == Path("test.db")
    assert args.benchmarks_dir == Path("specs")


@pytest.mark.asyncio
async def test_run_benchmark_all_filters_by_model_capabilities(
    monkeypatch, tmp_path, capsys
):
    coding = SimpleNamespace(
        id="coding.file_stats",
        required_capabilities=("completion",),
    )
    reasoning = SimpleNamespace(
        id="reasoning.logic",
        required_capabilities=("reasoning",),
    )

    models = [
        SimpleNamespace(
            name="model-coder",
            capabilities=["completion"],
            inspection_error=None,
        ),
        SimpleNamespace(
            name="model-reasoner",
            capabilities=["reasoning", "completion"],
            inspection_error=None,
        ),
    ]

    class FakeProvider:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def health_check(self):
            return {"version": "test"}

    calls = []

    class FakeOrchestrator:
        def __init__(self, *args):
            pass

        async def run_batch_for_model(self, model, benchmarks):
            calls.append((model, [item.id for item in benchmarks]))
            return [
                SimpleNamespace(
                    benchmark_id=item.id,
                    status="passed",
                    score=1.0,
                )
                for item in benchmarks
            ]


    monkeypatch.setattr(cli, "OllamaProvider", FakeProvider)
    monkeypatch.setattr(
        cli,
        "load_benchmarks",
        lambda *args, **kwargs: [coding, reasoning],
    )
    monkeypatch.setattr(
        cli,
        "discover_models",
        lambda provider: _return_models(models),
    )
    monkeypatch.setattr(cli, "ModelManager", lambda provider: object())
    monkeypatch.setattr(cli, "BenchmarkRunner", lambda provider: object())
    monkeypatch.setattr(cli, "BenchmarkStore", lambda path: object())
    monkeypatch.setattr(cli, "BenchmarkOrchestrator", FakeOrchestrator)

    result = await cli.run_benchmark_all(
        tmp_path / "test.db",
        tmp_path,
    )

    assert result == 0
    assert calls == [
        ("model-coder", ["coding.file_stats"]),
        ("model-reasoner", ["coding.file_stats", "reasoning.logic"]),
    ]

    captured = capsys.readouterr()
    assert "Benchmark eseguiti: 3" in captured.out


async def _return_models(models):
    return models


@pytest.mark.asyncio
async def test_run_benchmark_all_skips_models_with_inspection_errors(
    monkeypatch, tmp_path, capsys
):
    models = [
        SimpleNamespace(
            name="model-unavailable",
            capabilities=[],
            inspection_error="metadata unavailable",
        )
    ]

    class FakeProvider:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def health_check(self):
            return {"version": "test"}

    class FakeOrchestrator:
        def __init__(self, *args):
            pass

        async def run_batch_for_model(self, model, benchmarks):
            pytest.fail("Un modello con errori di ispezione non deve essere eseguito.")

    monkeypatch.setattr(cli, "OllamaProvider", FakeProvider)
    monkeypatch.setattr(
        cli,
        "load_benchmarks",
        lambda *args, **kwargs: [
            SimpleNamespace(
                id="coding.file_stats",
                required_capabilities=("completion",),
            )
        ],
    )
    monkeypatch.setattr(
        cli,
        "discover_models",
        lambda provider: _return_models(models),
    )
    monkeypatch.setattr(cli, "ModelManager", lambda provider: object())
    monkeypatch.setattr(cli, "BenchmarkRunner", lambda provider: object())
    monkeypatch.setattr(cli, "BenchmarkStore", lambda path: object())
    monkeypatch.setattr(cli, "BenchmarkOrchestrator", FakeOrchestrator)

    result = await cli.run_benchmark_all(tmp_path / "test.db", tmp_path)

    captured = capsys.readouterr()

    assert result == 1
    assert "metadati non disponibili" in captured.out
    assert "Benchmark saltati: 1" in captured.out


def test_run_benchmark_ranking_displays_all_rankings(monkeypatch, tmp_path, capsys):
    database = tmp_path / "results.db"
    database.touch()

    runs = [
        {"id": 1, "model": "model-a", "category": "coding"},
        {"id": 2, "model": "model-b", "category": "reasoning"},
    ]

    class FakeStore:
        def __init__(self, path):
            assert path == database

        def list_runs(self, limit=None):
            assert limit is None
            return runs

    monkeypatch.setattr(cli, "BenchmarkStore", FakeStore)
    monkeypatch.setattr(
        cli,
        "build_rankings",
        lambda data: {
            "categories": {
                "coding": [
                    {"rank": 1, "model": "model-a", "score": 90, "benchmarks_count": 2}
                ]
            },
            "overall": [
                {"rank": 1, "model": "model-a", "score": 90, "categories_count": 1}
            ],
            "efficiency": [
                {
                    "rank": 1,
                    "model": "model-a",
                    "generation_tokens_per_second": 30,
                    "total_duration_seconds": 2,
                    "load_duration_seconds": 0.5,
                }
            ],
        },
    )

    result = cli.run_benchmark_ranking(database)

    captured = capsys.readouterr()

    assert result == 0
    assert "RANKING PER CATEGORIA" in captured.out
    assert "RANKING GENERALE" in captured.out
    assert "RANKING EFFICIENZA" in captured.out
    assert "model-a" in captured.out


def test_run_benchmark_ranking_filters_category(monkeypatch, tmp_path, capsys):
    database = tmp_path / "results.db"
    database.touch()

    runs = [
        {"id": 1, "model": "model-a", "category": "coding"},
        {"id": 2, "model": "model-b", "category": "reasoning"},
    ]
    received_runs = []

    class FakeStore:
        def __init__(self, path):
            pass

        def list_runs(self, limit=None):
            return runs

    def fake_build_rankings(data):
        received_runs.extend(data)
        return {"categories": {}, "overall": [], "efficiency": []}

    monkeypatch.setattr(cli, "BenchmarkStore", FakeStore)
    monkeypatch.setattr(cli, "build_rankings", fake_build_rankings)

    result = cli.run_benchmark_ranking(database, category="coding")

    assert result == 0
    assert received_runs == [runs[0]]
    assert "Categoria selezionata: coding" in capsys.readouterr().out


def test_run_benchmark_ranking_rejects_missing_database(tmp_path, capsys):
    database = tmp_path / "missing.db"

    result = cli.run_benchmark_ranking(database)

    captured = capsys.readouterr()

    assert result == 1
    assert "Database non trovato" in captured.err


@pytest.mark.asyncio
async def test_run_benchmark_all_continues_after_ollama_error(
    monkeypatch, tmp_path, capsys
):
    models = [
        SimpleNamespace(
            name="model-broken",
            capabilities=["completion"],
            inspection_error=None,
        ),
        SimpleNamespace(
            name="model-working",
            capabilities=["completion"],
            inspection_error=None,
        ),
    ]

    benchmark = SimpleNamespace(
        id="coding.file_stats",
        required_capabilities=("completion",),
    )

    class FakeProvider:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def health_check(self):
            return {"version": "test"}

    calls = []

    class FakeOrchestrator:
        def __init__(self, *args):
            pass

        async def run_batch_for_model(self, model, benchmarks):
            calls.append(model)

            if model == "model-broken":
                raise cli.OllamaError("Errore simulato HTTP 500")

            return [
                SimpleNamespace(
                    benchmark_id=item.id,
                    status="passed",
                    score=1.0,
                )
                for item in benchmarks
            ]

    monkeypatch.setattr(cli, "OllamaProvider", FakeProvider)
    monkeypatch.setattr(
        cli, "load_benchmarks", lambda *args, **kwargs: [benchmark]
    )
    monkeypatch.setattr(
        cli, "discover_models", lambda provider: _return_models(models)
    )
    monkeypatch.setattr(cli, "ModelManager", lambda provider: object())
    monkeypatch.setattr(cli, "BenchmarkRunner", lambda provider: object())
    monkeypatch.setattr(cli, "BenchmarkStore", lambda path: object())
    monkeypatch.setattr(cli, "BenchmarkOrchestrator", FakeOrchestrator)

    result = await cli.run_benchmark_all(tmp_path / "test.db", tmp_path)

    captured = capsys.readouterr()

    assert result == 0
    assert calls == ["model-broken", "model-working"]
    assert "Modello saltato" in captured.out
    assert "Benchmark eseguiti: 1" in captured.out
    assert "Benchmark saltati: 1" in captured.out
    assert "Errore simulato HTTP 500" in captured.err
