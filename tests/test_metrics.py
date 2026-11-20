import pytest

from lar.evaluation.metrics import GenerationMetrics


def test_metrics_calculate_durations_and_throughput():
    response = {
        "total_duration": 5_000_000_000,
        "load_duration": 1_000_000_000,
        "prompt_eval_count": 100,
        "prompt_eval_duration": 2_000_000_000,
        "eval_count": 50,
        "eval_duration": 2_000_000_000,
    }

    metrics = GenerationMetrics.from_ollama_response(response)

    assert metrics.total_duration_seconds == 5.0
    assert metrics.load_duration_seconds == 1.0
    assert metrics.prompt_tokens_per_second == 50.0
    assert metrics.generation_tokens_per_second == 25.0


def test_missing_metrics_remain_none():
    metrics = GenerationMetrics.from_ollama_response({})

    assert metrics.total_duration_seconds is None
    assert metrics.load_duration_seconds is None
    assert metrics.prompt_tokens_per_second is None
    assert metrics.generation_tokens_per_second is None


def test_zero_duration_does_not_cause_division_by_zero():
    metrics = GenerationMetrics.from_ollama_response(
        {
            "eval_count": 10,
            "eval_duration": 0,
            "prompt_eval_count": 20,
            "prompt_eval_duration": 0,
        }
    )

    assert metrics.generation_tokens_per_second is None
    assert metrics.prompt_tokens_per_second is None


def test_invalid_values_are_ignored():
    metrics = GenerationMetrics.from_ollama_response(
        {
            "eval_count": -10,
            "eval_duration": "invalid",
            "total_duration": True,
        }
    )

    assert metrics.eval_count is None
    assert metrics.eval_duration_ns is None
    assert metrics.total_duration_ns is None


def test_metrics_can_be_serialized():
    metrics = GenerationMetrics.from_ollama_response(
        {
            "eval_count": 40,
            "eval_duration": 2_000_000_000,
        }
    )

    data = metrics.to_dict()

    assert data["eval_count"] == 40
    assert data["generation_tokens_per_second"] == pytest.approx(20.0)
