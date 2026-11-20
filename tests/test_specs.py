from pathlib import Path

import pytest
import yaml

from lar.evaluation.specs import (
    BenchmarkSpecError,
    load_benchmark,
    load_benchmarks,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_BENCHMARK = PROJECT_ROOT / "benchmarks" / "coding" / "file_stats.yaml"


def make_spec() -> dict:
    return {
        "id": "coding.test",
        "version": 1,
        "category": "coding",
        "name": "Test Benchmark",
        "description": "Benchmark di test.",
        "required_capabilities": ["completion"],
        "prompt": "Scrivi una funzione Python.",
        "evaluation": {
            "type": "python_unittest",
            "timeout_seconds": 10,
            "test_code": "import unittest",
        },
        "generation": {
            "temperature": 0,
            "seed": 42,
            "num_predict": 512,
        },
    }


def write_spec(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "benchmark.yaml"
    path.write_text(
        yaml.safe_dump(data, sort_keys=False),
        encoding="utf-8",
    )
    return path


def test_load_real_coding_benchmark():
    benchmark = load_benchmark(REAL_BENCHMARK)

    assert benchmark.id == "coding.file_stats"
    assert benchmark.version == 2
    assert benchmark.category == "coding"
    assert benchmark.evaluation.type == "python_unittest"
    assert benchmark.evaluation.timeout_seconds == 10
    assert "analyze_files" in benchmark.prompt
    assert "test_empty_list" in benchmark.evaluation.test_code


def test_load_benchmarks_filters_by_category():
    benchmarks = load_benchmarks(
        PROJECT_ROOT / "benchmarks",
        category="coding",
    )

    assert len(benchmarks) == 1
    assert benchmarks[0].id == "coding.file_stats"


def test_load_benchmark_reads_generation_options(tmp_path):
    path = write_spec(tmp_path, make_spec())

    benchmark = load_benchmark(path)

    assert benchmark.generation["temperature"] == 0
    assert benchmark.generation["seed"] == 42
    assert benchmark.generation["num_predict"] == 512


def test_unknown_generation_option_is_rejected(tmp_path):
    data = make_spec()
    data["generation"]["unknown_option"] = 10
    path = write_spec(tmp_path, data)

    with pytest.raises(BenchmarkSpecError, match="non riconosciute"):
        load_benchmark(path)


def test_invalid_timeout_is_rejected(tmp_path):
    data = make_spec()
    data["evaluation"]["timeout_seconds"] = 0
    path = write_spec(tmp_path, data)

    with pytest.raises(BenchmarkSpecError, match="timeout_seconds"):
        load_benchmark(path)


def test_unsupported_evaluation_type_is_rejected(tmp_path):
    data = make_spec()
    data["evaluation"]["type"] = "unknown"
    path = write_spec(tmp_path, data)

    with pytest.raises(BenchmarkSpecError, match="non supportato"):
        load_benchmark(path)


def test_missing_required_field_is_rejected(tmp_path):
    data = make_spec()
    del data["prompt"]
    path = write_spec(tmp_path, data)

    with pytest.raises(BenchmarkSpecError, match="prompt"):
        load_benchmark(path)


def test_malformed_yaml_is_rejected(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("id: [unclosed", encoding="utf-8")

    with pytest.raises(BenchmarkSpecError, match="Impossibile leggere"):
        load_benchmark(path)
