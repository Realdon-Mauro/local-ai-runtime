from dataclasses import asdict, dataclass
from typing import Any


def _read_counter(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None

    return value


@dataclass(frozen=True)
class GenerationMetrics:
    """Metriche prestazionali restituite da Ollama."""

    total_duration_ns: int | None
    load_duration_ns: int | None
    prompt_eval_count: int | None
    prompt_eval_duration_ns: int | None
    eval_count: int | None
    eval_duration_ns: int | None

    @classmethod
    def from_ollama_response(
        cls,
        response: dict[str, Any],
    ) -> "GenerationMetrics":
        return cls(
            total_duration_ns=_read_counter(response, "total_duration"),
            load_duration_ns=_read_counter(response, "load_duration"),
            prompt_eval_count=_read_counter(response, "prompt_eval_count"),
            prompt_eval_duration_ns=_read_counter(
                response, "prompt_eval_duration"
            ),
            eval_count=_read_counter(response, "eval_count"),
            eval_duration_ns=_read_counter(response, "eval_duration"),
        )

    @classmethod
    def from_usage(cls, usage: Any) -> "GenerationMetrics":
        """Crea metriche normalizzate indipendenti dal provider."""

        def to_nanoseconds(value: Any) -> int | None:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value < 0
            ):
                return None

            return round(value * 1_000_000_000)

        def valid_count(value: Any) -> int | None:
            if isinstance(value, bool) or not isinstance(value, int):
                return None

            if value < 0:
                return None

            return value

        return cls(
            total_duration_ns=to_nanoseconds(
                usage.total_duration_seconds
            ),
            load_duration_ns=to_nanoseconds(
                usage.load_duration_seconds
            ),
            prompt_eval_count=valid_count(usage.input_tokens),
            prompt_eval_duration_ns=to_nanoseconds(
                usage.input_duration_seconds
            ),
            eval_count=valid_count(usage.output_tokens),
            eval_duration_ns=to_nanoseconds(
                usage.output_duration_seconds
            ),
        )

    @staticmethod
    def _seconds(value: int | None) -> float | None:
        if value is None:
            return None
        return value / 1_000_000_000

    @staticmethod
    def _throughput(
        count: int | None,
        duration_ns: int | None,
    ) -> float | None:
        if count is None or duration_ns is None or duration_ns <= 0:
            return None

        return count / (duration_ns / 1_000_000_000)

    @property
    def total_duration_seconds(self) -> float | None:
        return self._seconds(self.total_duration_ns)

    @property
    def load_duration_seconds(self) -> float | None:
        return self._seconds(self.load_duration_ns)

    @property
    def prompt_tokens_per_second(self) -> float | None:
        return self._throughput(
            self.prompt_eval_count,
            self.prompt_eval_duration_ns,
        )

    @property
    def generation_tokens_per_second(self) -> float | None:
        return self._throughput(
            self.eval_count,
            self.eval_duration_ns,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "total_duration_seconds": self.total_duration_seconds,
            "load_duration_seconds": self.load_duration_seconds,
            "prompt_tokens_per_second": self.prompt_tokens_per_second,
            "generation_tokens_per_second": self.generation_tokens_per_second,
        }
