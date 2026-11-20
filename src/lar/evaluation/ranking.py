"""Generazione delle classifiche dei benchmark."""

from collections import defaultdict
from math import isfinite
from typing import Any

VALID_STATUSES = {"passed", "failed"}


def _valid_run(run: dict[str, Any]) -> bool:
    """Verifica che un'esecuzione contenga una valutazione utilizzabile."""

    if run.get("status") not in VALID_STATUSES:
        return False

    score = run.get("score")
    tests_run = run.get("tests_run")
    tests_passed = run.get("tests_passed")

    return (
        isinstance(score, (int, float))
        and not isinstance(score, bool)
        and isfinite(score)
        and 0 <= score <= 1
        and isinstance(tests_run, int)
        and not isinstance(tests_run, bool)
        and tests_run > 0
        and isinstance(tests_passed, int)
        and not isinstance(tests_passed, bool)
        and 0 <= tests_passed <= tests_run
    )


def _latest_valid_runs(
    runs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Seleziona l'ultima valutazione valida della versione più recente."""

    latest_versions: dict[str, int] = {}

    for run in runs:
        benchmark_id = run.get("benchmark_id")
        version = run.get("benchmark_version")

        if (
            not isinstance(benchmark_id, str)
            or not benchmark_id
            or not isinstance(version, int)
            or isinstance(version, bool)
            or version < 1
        ):
            continue

        latest_versions[benchmark_id] = max(
            version,
            latest_versions.get(benchmark_id, version),
        )

    candidates = [
        run
        for run in runs
        if run.get("benchmark_id") in latest_versions
        and run.get("benchmark_version")
        == latest_versions[run["benchmark_id"]]
        and isinstance(run.get("category"), str)
        and run["category"]
        and isinstance(run.get("model"), str)
        and run["model"]
        and _valid_run(run)
    ]

    candidates.sort(key=lambda item: item.get("id", 0), reverse=True)

    selected: dict[tuple[str, str], dict[str, Any]] = {}

    for run in candidates:
        key = (run["model"], run["benchmark_id"])
        selected.setdefault(key, run)

    return list(selected.values())


def build_category_rankings(
    runs: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Calcola le classifiche per categoria."""

    selected = _latest_valid_runs(runs)
    grouped: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for run in selected:
        grouped[run["category"]][run["model"]].append(run["score"])

    rankings = {}

    for category, models in sorted(grouped.items()):
        entries = [
            {
                "model": model,
                "score": sum(scores) / len(scores) * 100,
                "benchmarks_count": len(scores),
            }
            for model, scores in models.items()
        ]

        entries.sort(key=lambda item: (-item["score"], item["model"]))

        for position, entry in enumerate(entries, 1):
            entry["rank"] = position

        rankings[category] = entries

    return rankings


def build_overall_ranking(
    category_rankings: dict[str, list[dict[str, Any]]],
    weights: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Calcola una media ponderata delle categorie disponibili."""

    weights = weights or {}
    totals: dict[str, float] = defaultdict(float)
    weight_totals: dict[str, float] = defaultdict(float)
    category_counts: dict[str, int] = defaultdict(int)

    for category, entries in category_rankings.items():
        weight = weights.get(category, 1.0)

        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not isfinite(weight)
            or weight <= 0
        ):
            raise ValueError(f"Peso non valido per la categoria {category!r}.")

        for entry in entries:
            model = entry["model"]
            totals[model] += entry["score"] * weight
            weight_totals[model] += weight
            category_counts[model] += 1

    ranking = [
        {
            "model": model,
            "score": totals[model] / weight_totals[model],
            "categories_count": category_counts[model],
        }
        for model in totals
    ]

    ranking.sort(key=lambda item: (-item["score"], item["model"]))

    for position, entry in enumerate(ranking, 1):
        entry["rank"] = position

    return ranking


def build_efficiency_ranking(
    runs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Calcola le prestazioni medie dei modelli sulle valutazioni valide."""

    selected = _latest_valid_runs(runs)
    grouped: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )

    metric_fields = {
        "generation_tokens_per_second": "generation_tokens_per_second",
        "total_duration_seconds": "total_duration_seconds",
        "load_duration_seconds": "load_duration_seconds",
    }

    for run in selected:
        metrics = run.get("metrics") or {}

        for output_field, source_field in metric_fields.items():
            value = metrics.get(source_field)

            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and isfinite(value)
                and value >= 0
                and (value > 0 or output_field != "generation_tokens_per_second")
            ):
                grouped[run["model"]][output_field].append(value)

    ranking = []

    for model, metrics in grouped.items():
        throughput = metrics.get("generation_tokens_per_second", [])

        if not throughput:
            continue

        entry = {
            "model": model,
            "generation_tokens_per_second": sum(throughput) / len(throughput),
            "total_duration_seconds": (
                sum(metrics["total_duration_seconds"])
                / len(metrics["total_duration_seconds"])
                if metrics["total_duration_seconds"]
                else None
            ),
            "load_duration_seconds": (
                sum(metrics["load_duration_seconds"])
                / len(metrics["load_duration_seconds"])
                if metrics["load_duration_seconds"]
                else None
            ),
        }
        ranking.append(entry)

    ranking.sort(
        key=lambda item: (
            -item["generation_tokens_per_second"],
            item["model"],
        )
    )

    for position, entry in enumerate(ranking, 1):
        entry["rank"] = position

    return ranking


def build_rankings(
    runs: list[dict[str, Any]],
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Costruisce tutte le classifiche disponibili."""

    categories = build_category_rankings(runs)

    return {
        "categories": categories,
        "overall": build_overall_ranking(categories, weights),
        "efficiency": build_efficiency_ranking(runs),
    }
