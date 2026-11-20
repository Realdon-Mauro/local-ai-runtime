import json

import httpx
import pytest

from lar.providers.ollama import (
    OllamaConnectionError,
    OllamaError,
    OllamaProvider,
)


@pytest.mark.asyncio
async def test_health_check():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/version"
        return httpx.Response(200, json={"version": "0.40.11"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://test",
    )
    provider = OllamaProvider(client=client)

    result = await provider.health_check()

    assert result["version"] == "0.40.11"

    await client.aclose()


@pytest.mark.asyncio
async def test_list_models():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            json={"models": [{"name": "qwen2.5-coder:7b"}]},
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://test",
    )
    provider = OllamaProvider(client=client)

    models = await provider.list_models()

    assert len(models) == 1
    assert models[0]["name"] == "qwen2.5-coder:7b"

    await client.aclose()


@pytest.mark.asyncio
async def test_generate_sends_expected_payload():
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)

        assert request.url.path == "/api/generate"
        assert payload["model"] == "qwen2.5-coder:7b"
        assert payload["prompt"] == "Scrivi una funzione Python."
        assert payload["system"] == "Sei un programmatore."
        assert payload["options"]["temperature"] == 0.1
        assert payload["stream"] is False

        return httpx.Response(
            200,
            json={
                "response": "def esempio(): pass",
                "total_duration": 2_000_000_000,
                "load_duration": 500_000_000,
                "prompt_eval_count": 100,
                "prompt_eval_duration": 1_000_000_000,
                "eval_count": 50,
                "eval_duration": 2_000_000_000,
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://test",
    )
    provider = OllamaProvider(client=client)

    result = await provider.generate(
        model="qwen2.5-coder:7b",
        prompt="Scrivi una funzione Python.",
        system="Sei un programmatore.",
        options={"temperature": 0.1},
    )

    assert "def esempio" in result.text
    assert result.raw["response"] == result.text

    assert result.usage.total_duration_seconds == 2.0
    assert result.usage.load_duration_seconds == 0.5
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 50
    assert result.usage.input_tokens_per_second == 100.0
    assert result.usage.output_tokens_per_second == 25.0

    await client.aclose()


@pytest.mark.asyncio
async def test_unload_model_sends_keep_alive_zero():
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)

        assert payload["model"] == "qwen2.5-coder:7b"
        assert payload["keep_alive"] == 0

        return httpx.Response(200, json={"response": ""})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://test",
    )
    provider = OllamaProvider(client=client)

    await provider.unload_model("qwen2.5-coder:7b")

    await client.aclose()


@pytest.mark.asyncio
async def test_http_error_is_converted():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Errore interno")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://test",
    )
    provider = OllamaProvider(client=client)

    with pytest.raises(OllamaError, match="HTTP 500"):
        await provider.health_check()

    await client.aclose()


@pytest.mark.asyncio
async def test_connection_error_is_converted():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connessione rifiutata")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://test",
    )
    provider = OllamaProvider(client=client)

    with pytest.raises(OllamaConnectionError):
        await provider.health_check()

    await client.aclose()


@pytest.mark.asyncio
async def test_show_model_sends_expected_payload():
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)

        assert request.url.path == "/api/show"
        assert payload["model"] == "qwen2.5-coder:7b"
        assert payload["verbose"] is True

        return httpx.Response(
            200,
            json={
                "capabilities": ["completion", "tools"],
                "model_info": {"general.architecture": "qwen2"},
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://test",
    )
    provider = OllamaProvider(client=client)

    result = await provider.show_model("qwen2.5-coder:7b")

    assert "tools" in result["capabilities"]

    await client.aclose()
