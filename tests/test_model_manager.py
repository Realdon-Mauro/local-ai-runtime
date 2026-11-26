import asyncio
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from lar.core.model_manager import (
    ModelLease,
    ModelManager,
    ModelManagerError,
    ModelOwnership,
    ModelUnavailableError,
    ObservedPresence,
)
from lar.providers.ollama import OllamaHTTPError


class FakeOllamaProvider:
    def __init__(self):
        self.running = []
        self.load_calls = []
        self.unload_calls = []
        self.list_error = None
        self.load_error = None
        self.list_gate = None
        self.load_gate = None

    async def list_running(self):
        if self.list_gate:
            await self.list_gate.wait()
        if self.list_error:
            raise self.list_error
        return [{"name": name} for name in self.running]

    async def load_model(self, model):
        self.load_calls.append(model)
        if self.load_gate:
            await self.load_gate.wait()
        if self.load_error:
            raise self.load_error
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

    await manager.acquire("tinyllama:1.1b")
    state = manager.get_states()[0]

    assert state.loaded is True
    assert state.ownership is ModelOwnership.LAR
    assert state.observed_presence is ObservedPresence.PRESENT
    assert state.active_requests == 1
    assert provider.load_calls == ["tinyllama:1.1b"]


@pytest.mark.asyncio
async def test_acquire_does_not_load_already_running_model():
    provider = FakeOllamaProvider()
    provider.running.append("tinyllama:1.1b")
    manager = ModelManager(provider)

    await manager.acquire("tinyllama:1.1b")
    state = manager.get_states()[0]

    assert state.loaded is True
    assert state.ownership is ModelOwnership.EXTERNAL
    assert state.observed_presence is ObservedPresence.PRESENT
    assert state.active_requests == 1
    assert provider.load_calls == []


@pytest.mark.asyncio
async def test_unload_is_blocked_while_model_is_in_use():
    provider = FakeOllamaProvider()
    manager = ModelManager(provider)

    await manager.acquire("tinyllama:1.1b")

    with pytest.raises(ModelManagerError, match="non supportato"):
        await manager.unload("tinyllama:1.1b")

    assert provider.unload_calls == []


@pytest.mark.asyncio
async def test_lease_releases_model_after_use():
    provider = FakeOllamaProvider()
    manager = ModelManager(provider)

    async with manager.lease("tinyllama:1.1b") as lease:
        assert lease.model == "tinyllama:1.1b"
        assert manager.get_states()[0].active_requests == 1

    states = manager.get_states()

    assert states[0].active_requests == 0
    assert states[0].loaded is True


@pytest.mark.asyncio
async def test_unload_is_disabled_even_after_release_of_manager_loaded_model():
    provider = FakeOllamaProvider()
    manager = ModelManager(provider)

    lease = await manager.acquire("tinyllama:1.1b")
    await manager.release(lease)
    with pytest.raises(ModelManagerError, match="non supportato"):
        await manager.unload("tinyllama:1.1b")

    state = manager.get_states()[0]

    assert state.loaded is True
    assert state.active_requests == 0
    assert state.owned_by_manager is True
    assert provider.unload_calls == []


@pytest.mark.asyncio
async def test_http_404_during_load_is_definitive_unavailable_not_ambiguous():
    provider = FakeOllamaProvider()
    provider.load_error = OllamaHTTPError("model missing", status_code=404)
    manager = ModelManager(provider)
    with pytest.raises(ModelUnavailableError):
        await manager.acquire("deleted:model")
    state = manager.get_states()[0]
    assert state.observed_presence is ObservedPresence.ABSENT
    assert not manager._states["deleted:model"].load_outcome_ambiguous


@pytest.mark.asyncio
async def test_release_without_acquire_fails():
    provider = FakeOllamaProvider()
    manager = ModelManager(provider)

    await manager.release(ModelLease(uuid4(), "tinyllama:1.1b"))
    assert manager.get_states() == []


@pytest.mark.asyncio
async def test_lease_releases_model_even_if_operation_fails():
    provider = FakeOllamaProvider()
    manager = ModelManager(provider)

    with pytest.raises(RuntimeError):
        async with manager.lease("tinyllama:1.1b"):
            raise RuntimeError("Errore simulato")

    state = manager.get_states()[0]

    assert state.active_requests == 0
    assert provider.unload_calls == []


@pytest.mark.asyncio
async def test_release_never_unloads_previously_running_model():
    provider = FakeOllamaProvider()
    provider.running.append("tinyllama:1.1b")
    manager = ModelManager(provider)

    lease = await manager.acquire("tinyllama:1.1b")
    await manager.release(lease)

    state = manager.get_states()[0]

    assert state.loaded is True
    assert state.owned_by_manager is False
    assert provider.unload_calls == []
    assert "tinyllama:1.1b" in provider.running


@pytest.mark.asyncio
async def test_release_never_unloads_model_loaded_by_manager():
    provider = FakeOllamaProvider()
    manager = ModelManager(provider)

    lease = await manager.acquire("tinyllama:1.1b")
    state = await manager.release(lease)

    assert state.active_requests == 0
    assert state.loaded is True
    assert provider.unload_calls == []


def test_ownership_and_presence_have_three_explicit_values():
    assert {value.value for value in ModelOwnership} == {"lar", "external", "unknown"}
    assert {value.value for value in ObservedPresence} == {"present", "absent", "unknown"}


@pytest.mark.asyncio
async def test_new_state_has_unknown_ownership_and_presence():
    manager = ModelManager(FakeOllamaProvider())

    state = manager.get_states()

    assert state == []
    state = manager._get_state("unknown-model")
    assert state.ownership is ModelOwnership.UNKNOWN
    assert state.observed_presence is ObservedPresence.UNKNOWN


@pytest.mark.asyncio
async def test_provider_verification_error_marks_presence_unknown():
    provider = FakeOllamaProvider()
    provider.list_error = TimeoutError("timeout")
    manager = ModelManager(provider)

    with pytest.raises(TimeoutError):
        await manager.acquire("model")

    state = manager.get_states()[0]
    assert state.observed_presence is ObservedPresence.UNKNOWN
    assert state.ownership is ModelOwnership.UNKNOWN


@pytest.mark.asyncio
async def test_load_error_keeps_remote_presence_ambiguous_and_ownership_unknown():
    provider = FakeOllamaProvider()
    provider.load_error = TimeoutError("timeout")
    manager = ModelManager(provider)

    with pytest.raises(TimeoutError):
        await manager.acquire("model")

    state = manager.get_states()[0]
    assert state.observed_presence is ObservedPresence.UNKNOWN
    assert state.ownership is ModelOwnership.UNKNOWN
    assert state.active_requests == 0


@pytest.mark.asyncio
async def test_ambiguous_load_followed_by_presence_keeps_unknown_ownership():
    provider = FakeOllamaProvider()
    provider.load_error = TimeoutError("timeout")
    manager = ModelManager(provider)

    with pytest.raises(TimeoutError):
        await manager.acquire("model")
    provider.load_error = None
    provider.running.append("model")

    await manager.acquire("model")
    state = manager.get_states()[0]

    assert state.observed_presence is ObservedPresence.PRESENT
    assert state.ownership is ModelOwnership.UNKNOWN


@pytest.mark.asyncio
async def test_same_model_acquisitions_have_distinct_independent_leases():
    manager = ModelManager(FakeOllamaProvider())
    first = await manager.acquire("model")
    second = await manager.acquire("model")

    assert first.id != second.id
    assert first.model == second.model == "model"
    assert manager.get_states()[0].active_requests == 2
    await manager.release(first)
    assert manager.get_states()[0].active_requests == 1
    assert second.id in manager._leases


@pytest.mark.asyncio
async def test_double_release_is_idempotent():
    manager = ModelManager(FakeOllamaProvider())
    lease = await manager.acquire("model")

    await manager.release(lease)
    await manager.release(lease)

    assert manager.get_states()[0].active_requests == 0


@pytest.mark.asyncio
async def test_state_snapshots_are_immutable_and_cannot_change_manager():
    manager = ModelManager(FakeOllamaProvider())
    await manager.acquire("model")
    snapshot = manager.get_states()[0]

    with pytest.raises(FrozenInstanceError):
        snapshot.name = "other-model"
    with pytest.raises(FrozenInstanceError):
        snapshot.active_requests = 0

    assert manager.get_states()[0].name == "model"
    assert manager.get_states()[0].active_requests == 1


@pytest.mark.asyncio
async def test_get_states_returns_independent_immutable_snapshots():
    manager = ModelManager(FakeOllamaProvider())
    await manager.acquire("model")

    first = manager.get_states()
    second = manager.get_states()

    assert first == second
    assert first is not second
    assert first[0] is not second[0]
    with pytest.raises(FrozenInstanceError):
        first[0].ownership = ModelOwnership.EXTERNAL
    assert manager.get_states()[0].ownership is ModelOwnership.LAR


@pytest.mark.asyncio
async def test_lease_token_is_immutable_and_does_not_expose_lease_record():
    manager = ModelManager(FakeOllamaProvider())
    lease = await manager.acquire("model")
    registered = manager._leases[lease.id]

    with pytest.raises(FrozenInstanceError):
        lease.model = "other-model"
    assert registered is not lease
    assert registered.model == "model"
    assert manager.get_states()[0].active_requests == 1


@pytest.mark.asyncio
async def test_unknown_lease_release_is_idempotent_and_does_not_change_counter():
    manager = ModelManager(FakeOllamaProvider())
    lease = await manager.acquire("model")

    await manager.release(ModelLease(uuid4(), "model"))
    assert manager.get_states()[0].active_requests == 1
    await manager.release(lease)
    await manager.release(lease)
    assert manager.get_states()[0].active_requests == len(manager._leases) == 0


@pytest.mark.asyncio
async def test_wrong_model_on_lease_does_not_release_it():
    manager = ModelManager(FakeOllamaProvider())
    lease = await manager.acquire("model-a")
    wrong_model_lease = ModelLease(lease.id, "model-b")

    await manager.release(wrong_model_lease)

    assert manager.get_states()[0].active_requests == 1
    assert lease.id in manager._leases


@pytest.mark.asyncio
async def test_concurrent_release_of_same_lease_decrements_once():
    import asyncio

    manager = ModelManager(FakeOllamaProvider())
    lease = await manager.acquire("model")

    await asyncio.gather(*(manager.release(lease) for _ in range(20)))

    assert manager.get_states()[0].active_requests == 0
    assert manager._leases == {}


@pytest.mark.asyncio
async def test_lease_releases_after_cancellation():
    import asyncio

    manager = ModelManager(FakeOllamaProvider())
    entered = asyncio.Event()

    async def use_lease():
        async with manager.lease("model"):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(use_lease())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert manager.get_states()[0].active_requests == 0
    assert manager._leases == {}


@pytest.mark.asyncio
async def test_lease_count_matches_registered_leases_and_release_never_unloads():
    provider = FakeOllamaProvider()
    manager = ModelManager(provider)
    leases = [await manager.acquire("model") for _ in range(3)]

    for lease in leases:
        assert manager.get_states()[0].active_requests == sum(
            item.model == "model" for item in manager._leases.values()
        )
        await manager.release(lease)

    assert manager.get_states()[0].active_requests == len(manager._leases) == 0
    assert provider.unload_calls == []


@pytest.mark.asyncio
async def test_known_lar_ownership_is_preserved_when_model_is_present():
    provider = FakeOllamaProvider()
    manager = ModelManager(provider)
    lease = await manager.acquire("model")
    await manager.release(lease)

    await manager.acquire("model")
    observed = manager.get_states()[0]

    assert observed.ownership is ModelOwnership.LAR
    assert observed.observed_presence is ObservedPresence.PRESENT


@pytest.mark.asyncio
async def test_incomplete_provider_list_marks_presence_unknown():
    class IncompleteProvider(FakeOllamaProvider):
        async def list_running(self):
            return [{"id": "no-name"}]

    manager = ModelManager(IncompleteProvider())

    with pytest.raises(ValueError, match="incompleto"):
        await manager.acquire("model")

    assert manager.get_states()[0].observed_presence is ObservedPresence.UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ConnectionError("offline"), TimeoutError("timeout")])
async def test_verification_errors_keep_unknown_and_register_no_lease(error):
    provider = FakeOllamaProvider()
    provider.list_error = error
    manager = ModelManager(provider)
    with pytest.raises(type(error)):
        await manager.acquire("model")
    state = manager.get_states()[0]
    assert state.observed_presence is ObservedPresence.UNKNOWN
    assert state.ownership is ModelOwnership.UNKNOWN
    assert state.active_requests == len(manager._leases) == 0
    assert not manager._lock.locked()


@pytest.mark.asyncio
async def test_load_error_preserves_known_ownership_and_presence_as_unknown():
    provider = FakeOllamaProvider()
    manager = ModelManager(provider)
    lease = await manager.acquire("model")
    await manager.release(lease)
    provider.running.clear()
    provider.load_error = RuntimeError("load failed")
    with pytest.raises(RuntimeError, match="load failed"):
        await manager.acquire("model")
    state = manager.get_states()[0]
    assert state.ownership is ModelOwnership.LAR
    assert state.observed_presence is ObservedPresence.UNKNOWN
    assert state.active_requests == len(manager._leases) == 0
    assert provider.unload_calls == []
    assert not manager._lock.locked()


@pytest.mark.asyncio
async def test_cancellation_during_verification_propagates_and_unlocks():
    provider = FakeOllamaProvider()
    provider.list_gate = asyncio.Event()
    manager = ModelManager(provider)
    task = asyncio.create_task(manager.acquire("model"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    state = manager.get_states()[0]
    assert state.observed_presence is ObservedPresence.UNKNOWN
    assert state.active_requests == len(manager._leases) == 0
    assert provider.unload_calls == []
    assert not manager._lock.locked()


@pytest.mark.asyncio
async def test_cancellation_during_load_is_ambiguous_and_registers_no_lease():
    provider = FakeOllamaProvider()
    provider.load_gate = asyncio.Event()
    manager = ModelManager(provider)
    task = asyncio.create_task(manager.acquire("model"))
    while not provider.load_calls:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    state = manager.get_states()[0]
    assert state.observed_presence is ObservedPresence.UNKNOWN
    assert state.ownership is ModelOwnership.UNKNOWN
    assert state.active_requests == len(manager._leases) == 0
    assert provider.unload_calls == []
    assert not manager._lock.locked()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_mutate_state_or_leak_lock():
    provider = FakeOllamaProvider()
    provider.list_gate = asyncio.Event()
    manager = ModelManager(provider)
    first = asyncio.create_task(manager.acquire("model"))
    await asyncio.sleep(0)
    waiter = asyncio.create_task(manager.acquire("model"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    provider.list_gate.set()
    lease = await first
    state = manager.get_states()[0]
    assert state.active_requests == len(manager._leases) == 1
    assert provider.load_calls == ["model"]
    assert not manager._lock.locked()
    await manager.release(lease)


@pytest.mark.asyncio
async def test_concurrent_acquires_register_consistent_independent_leases():
    manager = ModelManager(FakeOllamaProvider())
    leases = await asyncio.gather(*(manager.acquire("model") for _ in range(8)))
    state = manager.get_states()[0]
    assert len({lease.id for lease in leases}) == len(leases)
    assert state.active_requests == len(manager._leases) == len(leases)
    for lease in leases:
        await manager.release(lease)
    assert manager.get_states()[0].active_requests == len(manager._leases) == 0


@pytest.mark.asyncio
async def test_reconcile_updates_presence_without_changing_ownership_or_leases():
    provider = FakeOllamaProvider()
    manager = ModelManager(provider)
    lease = await manager.acquire("lar-model")
    await manager.release(lease)
    # Set up known external/unknown provenance and one runtime-only model.
    manager._get_state("external-model").ownership = ModelOwnership.EXTERNAL
    manager._get_state("unknown-model")
    provider.running = ["lar-model", "external-model", "runtime-only"]

    result = await manager.reconcile()
    states = {state.name: state for state in manager.get_states()}

    assert result.present == ("external-model", "lar-model", "runtime-only")
    assert result.absent == ("unknown-model",)
    assert result.unknown == ()
    assert result.errors == ()
    assert states["lar-model"].ownership is ModelOwnership.LAR
    assert states["external-model"].ownership is ModelOwnership.EXTERNAL
    assert states["unknown-model"].ownership is ModelOwnership.UNKNOWN
    assert states["runtime-only"].ownership is ModelOwnership.UNKNOWN
    assert all(state.observed_presence is ObservedPresence.PRESENT
               for name, state in states.items() if name != "unknown-model")
    assert states["unknown-model"].observed_presence is ObservedPresence.ABSENT
    assert states["lar-model"].active_requests == 0
    assert provider.unload_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ConnectionError("offline"), TimeoutError("timeout")])
async def test_reconcile_errors_mark_presence_unknown_and_preserve_state(error):
    provider = FakeOllamaProvider()
    provider.running = ["model"]
    manager = ModelManager(provider)
    lease = await manager.acquire("model")
    manager._get_state("absent-model").ownership = ModelOwnership.LAR
    provider.list_error = error

    result = await manager.reconcile()
    state_map = {state.name: state for state in manager.get_states()}

    assert result.present == result.absent == ()
    assert result.unknown == ("absent-model", "model")
    assert result.errors and type(error).__name__ in result.errors[0]
    assert state_map["model"].observed_presence is ObservedPresence.UNKNOWN
    assert state_map["model"].ownership is ModelOwnership.EXTERNAL
    assert state_map["absent-model"].ownership is ModelOwnership.LAR
    assert state_map["model"].active_requests == 1
    assert len(manager._leases) == 1
    assert not manager._lock.locked()
    assert provider.unload_calls == []
    await manager.release(lease)


@pytest.mark.asyncio
async def test_reconcile_incomplete_response_is_not_treated_as_complete():
    class IncompleteProvider(FakeOllamaProvider):
        async def list_running(self):
            return [{"name": "visible"}, {"size": 123}]

    provider = IncompleteProvider()
    manager = ModelManager(provider)
    manager._get_state("tracked")

    result = await manager.reconcile()

    assert result.present == result.absent == ()
    assert result.unknown == ("tracked",)
    assert result.errors
    assert manager.get_states()[0].observed_presence is ObservedPresence.UNKNOWN


@pytest.mark.asyncio
async def test_reconcile_cancellation_propagates_unlocks_and_preserves_leases():
    provider = FakeOllamaProvider()
    manager = ModelManager(provider)
    lease = await manager.acquire("model")
    gate = asyncio.Event()
    provider.list_gate = gate
    task = asyncio.create_task(manager.reconcile())
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    state = manager.get_states()[0]
    assert state.observed_presence is ObservedPresence.UNKNOWN
    assert state.ownership is ModelOwnership.LAR
    assert state.active_requests == 1
    assert len(manager._leases) == 1
    assert not manager._lock.locked()
    assert provider.unload_calls == []
    await manager.release(lease)


@pytest.mark.asyncio
async def test_concurrent_reconciliations_return_consistent_immutable_snapshots():
    provider = FakeOllamaProvider()
    manager = ModelManager(provider)
    manager._get_state("known")
    gate = asyncio.Event()
    provider.list_gate = gate
    first = asyncio.create_task(manager.reconcile())
    second = asyncio.create_task(manager.reconcile())
    await asyncio.sleep(0)
    provider.running = ["observed"]
    gate.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result.present == second_result.present == ("observed",)
    assert first_result.absent == second_result.absent == ("known",)
    assert isinstance(first_result.present, tuple)
    assert manager.get_states() == manager.get_states()
    assert {state.name: state.observed_presence for state in manager.get_states()} == {
        "known": ObservedPresence.ABSENT,
        "observed": ObservedPresence.PRESENT,
    }
    assert provider.unload_calls == []
