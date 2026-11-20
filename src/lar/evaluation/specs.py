import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class BenchmarkSpecError(ValueError):
    """Specifica di benchmark non valida."""


@dataclass(frozen=True)
class EvaluationSpec:
    type: str
    timeout_seconds: int
    test_code: str


@dataclass(frozen=True)
class BenchmarkSpec:
    id: str
    version: int
    category: str
    name: str
    description: str
    required_capabilities: tuple[str, ...]
    prompt: str
    evaluation: EvaluationSpec
    generation: dict[str, int | float]


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BenchmarkSpecError(f"'{field}' deve essere un oggetto YAML.")

    return value


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkSpecError(f"'{field}' deve essere una stringa non vuota.")

    return value


def _require_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BenchmarkSpecError(f"'{field}' deve essere un intero positivo.")

    return value


def _parse_generation(value: Any) -> dict[str, int | float]:
    if value is None:
        return {}

    options = _require_mapping(value, "generation")
    allowed = {
        "temperature",
        "seed",
        "num_predict",
        "top_p",
        "top_k",
        "repeat_penalty",
    }

    unknown = set(options) - allowed
    if unknown:
        raise BenchmarkSpecError(
            f"Opzioni di generazione non riconosciute: {', '.join(sorted(unknown))}"
        )

    result: dict[str, int | float] = {}

    for key, item in options.items():
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise BenchmarkSpecError(f"generation.{key} deve essere numerico.")

        if not math.isfinite(item):
            raise BenchmarkSpecError(f"generation.{key} deve essere finito.")

        if key == "seed":
            if not isinstance(item, int) or item < 0:
                raise BenchmarkSpecError("generation.seed deve essere un intero >= 0.")

        elif key in {"num_predict", "top_k"}:
            if not isinstance(item, int) or item <= 0:
                raise BenchmarkSpecError(f"generation.{key} deve essere un intero positivo.")

        elif key == "temperature":
            if not 0 <= item <= 2:
                raise BenchmarkSpecError("generation.temperature deve essere tra 0 e 2.")

        elif key == "top_p":
            if not 0 < item <= 1:
                raise BenchmarkSpecError("generation.top_p deve essere maggiore di 0 e <= 1.")

        elif key == "repeat_penalty" and item <= 0:
            raise BenchmarkSpecError("generation.repeat_penalty deve essere positivo.")

        result[key] = item

    return result


def load_benchmark(path: str | Path) -> BenchmarkSpec:
    """Carica e valida una singola specifica YAML."""

    source = Path(path)

    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BenchmarkSpecError(f"Impossibile leggere {source}: {exc}") from exc

    data = _require_mapping(raw, "root")

    benchmark_id = _require_string(data.get("id"), "id")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", benchmark_id):
        raise BenchmarkSpecError("'id' contiene caratteri non validi.")

    version = _require_positive_int(data.get("version"), "version")
    category = _require_string(data.get("category"), "category")
    name = _require_string(data.get("name"), "name")
    description = _require_string(data.get("description"), "description")
    prompt = _require_string(data.get("prompt"), "prompt")

    capabilities = data.get("required_capabilities")
    if (
        not isinstance(capabilities, list)
        or not capabilities
        or any(not isinstance(item, str) or not item.strip() for item in capabilities)
    ):
        raise BenchmarkSpecError(
            "'required_capabilities' deve essere una lista di stringhe non vuote."
        )

    evaluation_data = _require_mapping(data.get("evaluation"), "evaluation")

    evaluation_type = _require_string(
        evaluation_data.get("type"),
        "evaluation.type",
    )

    if evaluation_type != "python_unittest":
        raise BenchmarkSpecError(
            f"Tipo di valutazione non supportato: {evaluation_type}"
        )

    timeout = _require_positive_int(
        evaluation_data.get("timeout_seconds"),
        "evaluation.timeout_seconds",
    )

    test_code = _require_string(
        evaluation_data.get("test_code"),
        "evaluation.test_code",
    )

    return BenchmarkSpec(
        id=benchmark_id,
        version=version,
        category=category,
        name=name,
        description=description,
        required_capabilities=tuple(capabilities),
        prompt=prompt,
        evaluation=EvaluationSpec(
            type=evaluation_type,
            timeout_seconds=timeout,
            test_code=test_code,
        ),
        generation=_parse_generation(data.get("generation")),
    )


def load_benchmarks(
    directory: str | Path,
    category: str | None = None,
) -> list[BenchmarkSpec]:
    """Carica tutte le specifiche YAML di una directory."""

    root = Path(directory)

    if not root.is_dir():
        raise BenchmarkSpecError(f"Directory benchmark inesistente: {root}")

    paths = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".yaml", ".yml"}
    )

    benchmarks = [load_benchmark(path) for path in paths]

    if category is not None:
        benchmarks = [
            benchmark
            for benchmark in benchmarks
            if benchmark.category == category
        ]

    return benchmarks
