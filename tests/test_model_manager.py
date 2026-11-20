import pytest

from lar.core.model_manager import (
    ModelInUseError,
    ModelManager,
    ModelManagerError,
)


class FakeOllamaProvider:
    def __init__(self):
        self.running = []
        self.load_calls = []
        self.unload_calls = []

    async def list_running(self):
        return [{"name": name} for name in self.running]

    async def load_model(self, model):
        self.load_calls.append(model)
        if model not in self.running:
            self.running.append(model)
        return {"done": True}

    async def unload_model(self, model):
        self.unload_calls.append(model)
        if model in self.running:
            self.running.remove(model)
        return {"done": True}


@pytest.mark.asyncio
async def test_acquire_loads_model():
    provider = FakeOllamaProvider()
    manager = ModelManager(provider)

    state = await manager.acquire("tinyllama:1.1b")

    assert state.loaded is True
    assert state.active_requests == 1
    assert provider.load_calls == ["tinyllama:1.1b"]


@pytest.mark.asyncio
async def test_acquire_does_not_load_already_running_model():
    provider = FakeOllamaProvider()
    provider.running.append("tinyllama:1.1b")
    manager = ModelManager(provider)

    state = await manager.acquire("tinyllama:1.1b")

    assert state.loaded is True
    assert state.active_requests == 1
    assert provider.load_calls == []


@pytest.mark.asyncio
async def test_unload_is_blocked_while_model_is_in_use():
    provider = FakeOllamaProvider()
    manager = ModelManager(provider)

    await manager.acquire("tinyllama:1.1b")

    with pytest.raises(ModelInUseError):
        await manager.unload("tinyllama:1.1b")

    assert provider.unload_calls == []


@pytest.mark.asyncio
async def test_lease_releases_model_after_use():
    provider = FakeOllamaProvider()
    manager = ModelManager(provider)

    async with manager.lease("tinyllama:1.1b") as state:
        assert state.active_requests == 1

    states = manager.get_states()

    assert states[0].active_requests == 0
    assert states[0].loaded is True


@pytest.mark.asyncio
async def test_unload_succeeds_after_release():
    provider = FakeOllamaProvider()
    manager = ModelManager(provider)

    await manager.acquire("tinyllama:1.1b")
    await manager.release("tinyllama:1.1b")
    await manager.unload("tinyllama:1.1b")

    state = manager.get_states()[0]

    assert state.loaded is False
    assert state.active_requests == 0
    assert provider.unload_calls == ["tinyllama:1.1b"]


@pytest.mark.asyncio
async def test_release_without_acquire_fails():
    provider = FakeOllamaProvider()
    manager = ModelManager(provider)

    with pytest.raises(ModelManagerError):
        await manager.release("tinyllama:1.1b")


@pytest.mark.asyncio
async def test_lease_releases_model_even_if_operation_fails():
    provider = FakeOllamaProvider()
    manager = ModelManager(provider)

    with pytest.raises(RuntimeError):
        async with manager.lease("tinyllama:1.1b"):
            raise RuntimeError("Errore simulato")

    state = manager.get_states()[0]

    assert state.active_requests == 0


@pytest.mark.asyncio
async def test_unload_preserves_model_already_running():
    provider = FakeOllamaProvider()
    provider.running.append("tinyllama:1.1b")
    manager = ModelManager(provider)

    await manager.acquire("tinyllama:1.1b")
    await manager.release("tinyllama:1.1b")
    await manager.unload("tinyllama:1.1b")

    state = manager.get_states()[0]

    assert state.loaded is True
    assert state.owned_by_manager is False
    assert provider.unload_calls == []
    assert "tinyllama:1.1b" in provider.running
