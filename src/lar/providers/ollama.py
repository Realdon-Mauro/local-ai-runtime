from typing import Any, Self

import httpx

from lar.providers.base import (
    GenerationResponse,
    GenerationUsage,
    ProviderConnectionError,
    ProviderError,
)


class OllamaError(ProviderError):
    """Errore generico del provider Ollama."""


class OllamaConnectionError(OllamaError, ProviderConnectionError):
    """Ollama non è raggiungibile."""


class OllamaProvider:
    """Client asincrono per le API locali di Ollama."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        timeout: float = 300.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=5.0),
        )

    async def close(self) -> None:
        """Chiude il client HTTP se gestito dal provider."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "Self":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _request(
        self,
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method,
                endpoint,
                **kwargs,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.RequestError as exc:
            raise OllamaConnectionError(
                f"Impossibile contattare Ollama: {exc}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise OllamaError(
                f"Ollama ha restituito HTTP {exc.response.status_code}: "
                f"{exc.response.text}"
            ) from exc
        except ValueError as exc:
            raise OllamaError("Ollama ha restituito una risposta JSON non valida.") from exc

        if not isinstance(data, dict):
            raise OllamaError("Formato della risposta Ollama non valido.")

        return data

    async def health_check(self) -> dict[str, Any]:
        """Restituisce la versione di Ollama se il servizio è raggiungibile."""
        return await self._request("GET", "/api/version")

    async def list_models(self) -> list[dict[str, Any]]:
        """Elenca i modelli installati."""
        data = await self._request("GET", "/api/tags")
        return data.get("models", [])

    async def list_running(self) -> list[dict[str, Any]]:
        """Elenca i modelli attualmente caricati in memoria."""
        data = await self._request("GET", "/api/ps")
        return data.get("models", [])

    async def load_model(
        self,
        model: str,
        keep_alive: str | int = -1,
    ) -> dict[str, Any]:
        """Carica un modello senza richiedere una generazione."""
        return await self._request(
            "POST",
            "/api/generate",
            json={
                "model": model,
                "prompt": "",
                "stream": False,
                "keep_alive": keep_alive,
            },
        )

    async def unload_model(self, model: str) -> dict[str, Any]:
        """Scarica un modello dalla memoria."""
        return await self._request(
            "POST",
            "/api/generate",
            json={
                "model": model,
                "prompt": "",
                "stream": False,
                "keep_alive": 0,
            },
        )

    @staticmethod
    def _read_counter(data: dict[str, Any], key: str) -> int | None:
        value = data.get(key)

        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None

        return value

    @classmethod
    def _read_seconds(cls, data: dict[str, Any], key: str) -> float | None:
        value = cls._read_counter(data, key)

        if value is None:
            return None

        return value / 1_000_000_000

    async def generate(
        self,
        model: str,
        prompt: str,
        system: str | None = None,
        options: dict[str, Any] | None = None,
        keep_alive: str | int = "5m",
    ) -> GenerationResponse:
        """Genera testo e normalizza le metriche restituite da Ollama."""
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": keep_alive,
        }

        if system is not None:
            payload["system"] = system

        if options is not None:
            payload["options"] = options

        raw = await self._request(
            "POST",
            "/api/generate",
            json=payload,
        )

        text = raw.get("response")

        if not isinstance(text, str):
            text = ""

        usage = GenerationUsage(
            total_duration_seconds=self._read_seconds(
                raw, "total_duration"
            ),
            load_duration_seconds=self._read_seconds(
                raw, "load_duration"
            ),
            input_tokens=self._read_counter(
                raw, "prompt_eval_count"
            ),
            input_duration_seconds=self._read_seconds(
                raw, "prompt_eval_duration"
            ),
            output_tokens=self._read_counter(
                raw, "eval_count"
            ),
            output_duration_seconds=self._read_seconds(
                raw, "eval_duration"
            ),
        )

        return GenerationResponse(
            text=text,
            usage=usage,
            raw=raw,
        )

	
    async def show_model(self, model: str) -> dict[str, Any]:
        """Recupera i metadati dettagliati di un modello."""
        return await self._request(
            "POST",
            "/api/show",
            json={
                "model": model,
                "verbose": True,
            },
        )
