from dataclasses import dataclass
from typing import Any, Protocol


class ProviderError(Exception):
    """Errore generico indipendente dal runtime."""


class ProviderConnectionError(ProviderError):
    """Errore di connessione a un provider."""


@dataclass(frozen=True)
class GenerationUsage:
    """Metriche di generazione normalizzate."""

    total_duration_seconds: float | None = None
    load_duration_seconds: float | None = None
    input_tokens: int | None = None
    input_duration_seconds: float | None = None
    output_tokens: int | None = None
    output_duration_seconds: float | None = None

    @staticmethod
    def _throughput(
        tokens: int | None,
        duration: float | None,
    ) -> float | None:
        if tokens is None or duration is None or duration <= 0:
            return None

        return tokens / duration

    @property
    def input_tokens_per_second(self) -> float | None:
        return self._throughput(
            self.input_tokens,
            self.input_duration_seconds,
        )

    @property
    def output_tokens_per_second(self) -> float | None:
        return self._throughput(
            self.output_tokens,
            self.output_duration_seconds,
        )


@dataclass(frozen=True)
class GenerationResponse:
    """Risposta normalizzata prodotta da un modello."""

    text: str
    usage: GenerationUsage
    raw: dict[str, Any]


class GenerationProvider(Protocol):
    """Contratto comune per i provider di generazione."""

    async def generate(
        self,
        model: str,
        prompt: str,
        system: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> GenerationResponse:
        """Genera una risposta utilizzando il modello indicato."""
        ...
