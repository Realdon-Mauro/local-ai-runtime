import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from lar.api.gateway import create_app
from lar.core.application import LARApplication, ProviderStopUnresolvedError
from lar.core.config import RuntimeConfig
from lar.providers.base import ChatChunk, ChatResponse, GenerationUsage
from lar.providers.ollama import OllamaHTTPError


class FakeProvider:
    def __init__(self):
        self.running: list[str] = []
        self.chat_requests = []
        self.closed = False

    async def health_check(self):
        return {"version": "test"}

    async def list_models(self):
        return [{"name": "physical-test", "digest": "digest-test", "size": 1024}]

    async def show_model(self, model):
        assert model == "physical-test"
        return {"capabilities": ["tools", "vision", "json"], "model_info": {"llama.context_length": 4096}}

    async def list_running(self):
        return [{"name": model} for model in self.running]

    async def load_model(self, model):
        self.running.append(model)
        return {"done": True}

    async def close(self):
        self.closed = True

    async def chat(self, request):
        self.chat_requests.append(request)
        return ChatResponse(
            message={"role": "assistant", "content": "hello"},
            usage=GenerationUsage(input_tokens=2, output_tokens=3),
            done_reason="stop",
            raw={},
        )

    async def stream_chat(self, request) -> AsyncIterator[ChatChunk]:
        self.chat_requests.append(request)
        yield ChatChunk(message={"role": "assistant", "content": "hel"}, done=False)
        yield ChatChunk(
            message={"role": "assistant", "content": "lo"},
            done=True,
            usage=GenerationUsage(input_tokens=2, output_tokens=2),
            done_reason="stop",
        )


@asynccontextmanager
async def make_client(tmp_path, *, token=None, opencode_token_file=None, provider=None):
    provider = provider or FakeProvider()
    config = RuntimeConfig(
        database_path=tmp_path / "lar-runtime.sqlite3",
        api_token=token,
        opencode_token_file=opencode_token_file or tmp_path / "missing-opencode.token",
    )
    runtime = LARApplication(config, provider=provider)
    app = create_app(config, application=runtime)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, provider


@pytest.mark.asyncio
async def test_openai_models_nonstream_and_alias_only(tmp_path, monkeypatch):
    monkeypatch.setattr("lar.core.resources.shutil.which", lambda _name: None)
    async with make_client(tmp_path) as (client, provider):
        models = await client.get("/v1/models")
        assert models.status_code == 200
        assert [item["id"] for item in models.json()["data"]] == ["lar-auto"]

        response = await client.post(
            "/v1/chat/completions",
            json={"model": "lar-auto", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["choices"][0]["message"]["content"] == "hello"
        assert result["usage"] == {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
        assert provider.chat_requests[0]["model"] == "physical-test"
        assert response.headers["x-request-id"]
        with sqlite3.connect(tmp_path / "lar-runtime.sqlite3") as connection:
            row = connection.execute(
                "SELECT status, client_model_alias, stream, content_persisted, selected_artifact FROM inference_requests WHERE request_id=?",
                (response.headers["x-request-id"],),
            ).fetchone()
            counts = connection.execute(
                "SELECT (SELECT count(*) FROM routing_decisions), "
                "(SELECT count(*) FROM routing_candidates), "
                "(SELECT count(*) FROM resource_snapshots), "
                "(SELECT count(*) FROM model_artifacts)"
            ).fetchone()
            metrics = connection.execute(
                "SELECT queue_wait_ms,first_token_ms,total_ms,prompt_tokens,output_tokens,"
                "generation_tps,sources_json FROM request_metrics WHERE request_id=?",
                (response.headers["x-request-id"],),
            ).fetchone()
        assert row[:4] == ("completed", "lar-auto", 0, 0)
        assert row[4] is not None
        assert counts == (1, 1, 1, 1)
        assert metrics[0] is not None and metrics[2] is not None
        assert metrics[1] is None
        assert metrics[3:6] == (2, 3, None)
        assert "no_continuous_sampling" in metrics[6]

        denied = await client.post(
            "/v1/chat/completions",
            json={"model": "physical-test", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert denied.status_code == 400
        assert denied.json()["error"]["code"] == "model_not_found"


@pytest.mark.asyncio
async def test_streaming_sse_chunks_usage_and_done_once(tmp_path, monkeypatch):
    monkeypatch.setattr("lar.core.resources.shutil.which", lambda _name: None)
    async with make_client(tmp_path) as (client, provider):
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "lar-auto",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.text.count("data: [DONE]") == 1
        assert '"content":"hel"' in response.text
        assert '"content":"lo"' in response.text
        assert '"prompt_tokens":2' in response.text
        assert provider.chat_requests[0]["stream"] is True
        with sqlite3.connect(tmp_path / "lar-runtime.sqlite3") as connection:
            metrics = connection.execute(
                "SELECT first_token_ms,prompt_tokens,output_tokens FROM request_metrics WHERE request_id=?",
                (response.headers["x-request-id"],),
            ).fetchone()
        assert metrics[0] is not None
        assert metrics[1:] == (2, 2)


@pytest.mark.asyncio
async def test_validation_openai_error_and_optional_bearer_auth(tmp_path):
    async with make_client(tmp_path, token="local-secret") as (client, _provider):
        unauthorized = await client.get("/v1/models")
        assert unauthorized.status_code == 401
        assert unauthorized.json()["error"]["code"] == "authentication_error"
        assert (await client.get("/health/live")).status_code == 200
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer local-secret"},
            json={"model": "lar-auto", "messages": []},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_opencode_observation_endpoint_is_private_idempotent_and_assigns_workflow(tmp_path):
    token_file = tmp_path / "integrations" / "opencode.token"
    token_file.parent.mkdir()
    token_file.write_text("dedicated-local-token\n", encoding="utf-8")
    async with make_client(tmp_path, opencode_token_file=token_file) as (client, _provider):
        url = "/integrations/opencode/v1/events"
        event = {
            "source": "plugin",
            "native_id": "message-001",
            "session_id": "session-001",
            "event_type": "user_message_received",
            "observed_at": "2026-11-26T12:00:00Z",
            "status": "started",
            "history_status": "unknown",
        }
        assert (await client.post(url, json=event)).status_code == 401
        headers = {"Authorization": "Bearer dedicated-local-token"}
        first = await client.post(url, json=event, headers=headers)
        assert first.status_code == 200
        result = first.json()
        assert result["status"] == "accepted"
        assert result["workflow_id"]
        duplicate = await client.post(url, json=event, headers=headers)
        assert duplicate.json()["status"] == "duplicate"
        assert duplicate.json()["workflow_id"] == result["workflow_id"]
        unsafe = await client.post(url, json={**event, "prompt": "must-not-persist"}, headers=headers)
        assert unsafe.status_code == 400
        with sqlite3.connect(tmp_path / "lar-runtime.sqlite3") as connection:
            rows = connection.execute(
                "SELECT workflow_id,safe_payload_json FROM observation_events"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == result["workflow_id"]
        assert "must-not-persist" not in rows[0][1]


@pytest.mark.asyncio
async def test_runtime_restart_stays_blocked_on_unresolved_provider_stop(tmp_path):
    config = RuntimeConfig(database_path=tmp_path / "lar-runtime.sqlite3")
    runtime = LARApplication(config, provider=FakeProvider())
    runtime.database.migrate()
    runtime.database.record_request("uncertain", stream=False, tool_count=0)
    runtime.database.update_request(
        "uncertain", status="provider_stop_unknown", error_code="provider_stop_unverified"
    )
    with pytest.raises(ProviderStopUnresolvedError, match="Avvio bloccato"):
        await runtime.start()
    assert runtime.provider.closed


@pytest.mark.asyncio
async def test_model_load_404_falls_back_before_generation_and_persists_choice(tmp_path, monkeypatch):
    class ProviderWithStalePrimary(FakeProvider):
        async def list_models(self):
            return [
                {"name": "a-model", "digest": "digest-a", "size": 1024},
                {"name": "b-model", "digest": "digest-b", "size": 2048},
            ]

        async def show_model(self, model):
            return {"capabilities": ["tools", "vision", "json"],
                    "model_info": {"llama.context_length": 4096}}

        async def load_model(self, model):
            if model == "a-model":
                raise OllamaHTTPError("model disappeared", status_code=404)
            await super().load_model(model)

    monkeypatch.setattr("lar.core.resources.shutil.which", lambda _name: None)
    provider = ProviderWithStalePrimary()
    async with make_client(tmp_path, provider=provider) as (client, _provider):
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "lar-auto", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200, response.text
        assert provider.chat_requests[0]["model"] == "b-model"
        with sqlite3.connect(tmp_path / "lar-runtime.sqlite3") as connection:
            selected_model = connection.execute(
                "SELECT a.provider_model_id FROM inference_requests r JOIN model_artifacts a "
                "ON a.artifact_id=r.selected_artifact WHERE r.request_id=?",
                (response.headers["x-request-id"],),
            ).fetchone()
            fallback = connection.execute(
                "SELECT event_type,details_json FROM model_lifecycle_events WHERE request_id=?",
                (response.headers["x-request-id"],),
            ).fetchone()
        assert selected_model == ("b-model",)
        assert fallback[0] == "pre_inference_model_fallback"
        assert '"reason": "primary_load_http_404"' in fallback[1]
