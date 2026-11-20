import pytest

from lar.evaluation.discovery import discover_models
from lar.providers.ollama import OllamaError


class FakeOllamaProvider:
    async def list_models(self):
        return [
            {
                "name": "qwen2.5-coder:7b",
                "digest": "abc123",
                "size": 4_700_000_000,
                "modified_at": "2026-11-16T10:00:00Z",
                "details": {
                    "format": "gguf",
                    "family": "qwen2",
                    "families": ["qwen2"],
                    "parameter_size": "7.6B",
                    "quantization_level": "Q4_K_M",
                },
            }
        ]

    async def show_model(self, model):
        assert model == "qwen2.5-coder:7b"

        return {
            "capabilities": ["completion", "tools"],
            "model_info": {
                "general.architecture": "qwen2",
            },
        }


class PartiallyFailingProvider(FakeOllamaProvider):
    async def show_model(self, model):
        raise OllamaError("Errore simulato")

@pytest.mark.asyncio
async def test_discover_models_collects_metadata():
    provider = FakeOllamaProvider()

    models = await discover_models(provider)

    assert len(models) == 1

    model = models[0]

    assert model.name == "qwen2.5-coder:7b"
    assert model.parameter_size == "7.6B"
    assert model.quantization_level == "Q4_K_M"
    assert model.capabilities == ["completion", "tools"]
    assert model.model_info["general.architecture"] == "qwen2"


@pytest.mark.asyncio
async def test_discovery_continues_when_model_inspection_fails():
    provider = PartiallyFailingProvider()

    models = await discover_models(provider)

    assert len(models) == 1
    assert models[0].name == "qwen2.5-coder:7b"
    assert models[0].inspection_error == "Errore simulato"
    assert models[0].capabilities == []


def test_model_entry_can_be_converted_to_dictionary():

    # Verifichiamo la struttura di serializzazione attraverso il risultato
    # prodotto dal discovery, senza dipendere da Ollama.
    from lar.evaluation.discovery import ModelInventoryEntry

    entry = ModelInventoryEntry(name="tinyllama:1.1b")
    data = entry.to_dict()

    assert data["name"] == "tinyllama:1.1b"
    assert data["size_bytes"] == 0
    assert data["capabilities"] == []
