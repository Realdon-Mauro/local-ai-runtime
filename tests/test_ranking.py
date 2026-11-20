import pytest

from lar.evaluation.ranking import (
    build_category_rankings,
    build_efficiency_ranking,
    build_overall_ranking,
    build_rankings,
)


def make_run(
    run_id,
    model="model-a",
    benchmark_id="coding.file_stats",
    version=1,
    category="coding",
    status="passed",
    score=1.0,
    tests_run=10,
    tests_passed=10,
    metrics=None,
):
    return {
        "id": run_id,
        "model": model,
        "benchmark_id": benchmark_id,
        "benchmark_version": version,
        "category": category,
        "status": status,
        "score": score,
        "tests_run": tests_run,
        "tests_passed": tests_passed,
        "metrics": metrics or {},
    }


def test_category_ranking_orders_models_by_average_score():
    runs = [
        make_run(1, model="model-a", score=0.8),
        make_run(2, model="model-b", score=0.9),
        make_run(
            3,
            model="model-a",
            benchmark_id="coding.other",
            score=0.6,
        ),
    ]

    ranking = build_category_rankings(runs)

    assert ranking["coding"][0]["model"] == "model-b"
    assert ranking["coding"][0]["score"] == pytest.approx(90)
    assert ranking["coding"][1]["model"] == "model-a"
    assert ranking["coding"][1]["score"] == pytest.approx(70)
    assert ranking["coding"][1]["benchmarks_count"] == 2


def test_ranking_uses_latest_version_and_latest_valid_execution():
    runs = [
        make_run(1, version=1, score=0.9),
        make_run(2, version=2, score=0.5),
        make_run(3, version=2, score=0.7, status="failed", tests_passed=7),
        make_run(4, version=2, score=None, status="generation_error"),
    ]

    ranking = build_category_rankings(runs)

    assert len(ranking["coding"]) == 1
    assert ranking["coding"][0]["score"] == pytest.approx(70)


def test_partial_failures_are_included_but_invalid_runs_are_excluded():
    runs = [
        make_run(1, score=0.7, status="failed", tests_passed=7),
        make_run(2, score=None, status="generation_error"),
        make_run(3, score=0.8, status="timeout"),
        make_run(4, score=0.9, tests_run=0, tests_passed=0),
        make_run(5, score=1.2),
        make_run(6, score=True),
    ]

    ranking = build_category_rankings(runs)

    assert len(ranking["coding"]) == 1
    assert ranking["coding"][0]["score"] == pytest.approx(70)


def test_models_without_category_results_are_not_penalized():
    runs = [
        make_run(1, model="coding-model", score=0.9),
        make_run(
            2,
            model="reasoning-model",
            benchmark_id="reasoning.logic",
            category="reasoning",
            score=0.8,
        ),
    ]

    rankings = build_rankings(runs)

    assert len(rankings["categories"]["coding"]) == 1
    assert len(rankings["categories"]["reasoning"]) == 1
    assert len(rankings["overall"]) == 2


def test_overall_ranking_supports_category_weights():
    categories = {
        "coding": [
            {"model": "model-a", "score": 80},
            {"model": "model-b", "score": 90},
        ],
        "reasoning": [
            {"model": "model-a", "score": 60},
        ],
    }

    ranking = build_overall_ranking(
        categories,
        weights={"coding": 2, "reasoning": 1},
    )

    assert ranking[0]["model"] == "model-b"
    assert ranking[0]["score"] == pytest.approx(90)
    assert ranking[1]["model"] == "model-a"
    assert ranking[1]["score"] == pytest.approx(80 / 3 * 2 + 60 / 3)


@pytest.mark.parametrize("weight", [0, -1, True, float("inf"), "invalid"])
def test_overall_ranking_rejects_invalid_weights(weight):
    categories = {
        "coding": [{"model": "model-a", "score": 80}],
    }

    with pytest.raises(ValueError):
        build_overall_ranking(categories, weights={"coding": weight})


def test_efficiency_ranking_averages_available_metrics():
    runs = [
        make_run(
            1,
            model="model-a",
            metrics={
                "generation_tokens_per_second": 20.0,
                "total_duration_seconds": 5.0,
                "load_duration_seconds": 1.0,
            },
        ),
        make_run(
            2,
            model="model-a",
            benchmark_id="coding.other",
            metrics={
                "generation_tokens_per_second": 40.0,
                "total_duration_seconds": 3.0,
                "load_duration_seconds": 0.5,
            },
        ),
        make_run(
            3,
            model="model-b",
            metrics={"generation_tokens_per_second": 50.0},
        ),
    ]

    ranking = build_efficiency_ranking(runs)

    assert ranking[0]["model"] == "model-b"
    assert ranking[1]["model"] == "model-a"
    assert ranking[1]["generation_tokens_per_second"] == pytest.approx(30)
    assert ranking[1]["total_duration_seconds"] == pytest.approx(4)
    assert ranking[1]["load_duration_seconds"] == pytest.approx(0.75)


def test_efficiency_ranking_excludes_models_without_throughput():
    runs = [
        make_run(
            1,
            metrics={"total_duration_seconds": 5.0},
        )
    ]

    assert build_efficiency_ranking(runs) == []


def test_build_rankings_returns_all_three_rankings():
    runs = [
        make_run(
            1,
            metrics={"generation_tokens_per_second": 25.0},
        )
    ]

    rankings = build_rankings(runs)

    assert set(rankings) == {"categories", "overall", "efficiency"}
    assert rankings["categories"]["coding"][0]["model"] == "model-a"
    assert rankings["overall"][0]["model"] == "model-a"
    assert rankings["efficiency"][0]["model"] == "model-a"
