from datetime import UTC, datetime, timedelta

import pytest

from lar.core.resources import (
    AdmissionLevel,
    DataOrigin,
    GpuSample,
    ResourceEstimate,
    ResourceManager,
    ResourcePolicy,
    ResourceRange,
    ResourceSnapshot,
)


def make_snapshot(*, age_seconds=0, available=10_000, gpus=()):
    return ResourceSnapshot(
        snapshot_id="snapshot-1",
        hardware_fingerprint="host-1",
        sampled_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
        cpu_utilization_percent=10.0,
        ram_total_bytes=20_000,
        ram_available_bytes=available,
        gpus=tuple(gpus),
        temperatures=(),
        external_processes=(),
        disk_total_bytes=100_000,
        disk_free_bytes=50_000,
        loaded_models=(),
        sources={"ram": "psutil:measured", "cpu": "psutil:measured"},
    )


@pytest.mark.asyncio
async def test_snapshot_records_unavailable_sensors_and_loaded_models(monkeypatch, tmp_path):
    class Provider:
        async def list_running(self):
            return [{"name": "external-model", "size_vram": 100}, {"name": "partial"}]

    monkeypatch.setattr("lar.core.resources.shutil.which", lambda _name: None)
    manager = ResourceManager(storage_path=tmp_path, provider=Provider())
    snapshot = await manager.snapshot()
    assert snapshot.sources["gpu"] == "unavailable"
    assert snapshot.sources["loaded_models"] == "ollama:/api/ps"
    assert snapshot.loaded_models[0].size_vram_bytes == 100
    assert snapshot.loaded_models[1].size_bytes is None
    assert snapshot.sources["temperature"] == "unavailable" or snapshot.temperatures
    assert all(process.rss_bytes is None or process.rss_bytes >= 0 for process in snapshot.external_processes or ())


def test_estimator_uses_kv_formula_only_with_complete_metadata():
    snapshot = make_snapshot()
    estimate = ResourceManager.estimate(
        snapshot=snapshot,
        artifact_size_bytes=1000,
        model_info={
            "llama.block_count": 2,
            "llama.attention.head_count_kv": 4,
            "lar.head_dimension": 8,
            "lar.kv_cache_bytes_per_element": 2,
        },
        requested_context_tokens=10,
        sequences=2,
    )
    assert estimate.kv_cache_bytes == 2 * 2 * 4 * 8 * 2 * 10 * 2
    assert estimate.declared_artifact_size_bytes == 1000
    assert estimate.ram.upper_bytes is None

    missing = ResourceManager.estimate(snapshot=snapshot, artifact_size_bytes=None)
    assert missing.kv_cache_bytes is None
    assert missing.declared_artifact_size_bytes is None
    assert missing.ram.lower_bytes is None


def test_admission_safe_constrained_and_rejected_are_explicit():
    snapshot = make_snapshot(available=1000)
    known = ResourceManager.estimate(
        snapshot=snapshot,
        artifact_size_bytes=500,
        model_info={
            "llama.block_count": 1,
            "llama.attention.head_count_kv": 1,
            "lar.head_dimension": 1,
            "lar.kv_cache_bytes_per_element": 1,
            "lar.estimated_ram_lower_bytes": 100,
            "lar.estimated_ram_upper_bytes": 200,
            "lar.resource_estimate_confidence": 0.9,
        },
        requested_context_tokens=10,
    )
    safe = ResourceManager.admit(snapshot=snapshot, estimate=known, policy=ResourcePolicy())
    assert safe.status is AdmissionLevel.SAFE
    assert safe.ram_margin_bytes == 800
    assert safe.can_execute

    unknown = ResourceManager.estimate(snapshot=snapshot, artifact_size_bytes=None)
    constrained = ResourceManager.admit(
        snapshot=snapshot, estimate=unknown, policy=ResourcePolicy()
    )
    assert constrained.status is AdmissionLevel.CONSTRAINED
    assert "ram_upper_bound_unknown" in constrained.reasons

    too_large = ResourceManager.estimate(
        snapshot=snapshot,
        artifact_size_bytes=500,
        model_info={
            "lar.estimated_ram_lower_bytes": 901,
            "lar.estimated_ram_upper_bytes": 1200,
        },
    )
    rejected = ResourceManager.admit(
        snapshot=snapshot, estimate=too_large, policy=ResourcePolicy(ram_reserve_bytes=100)
    )
    assert rejected.status is AdmissionLevel.REJECTED
    assert rejected.reasons == ("ram_lower_bound_exceeds_available",)


def test_admission_respects_snapshot_freshness_and_temperature_trip():
    stale = make_snapshot(age_seconds=100)
    estimate = ResourceEstimate(
        estimate_id="e1",
        snapshot_id=stale.snapshot_id,
        ram=ResourceRange(10, 20, DataOrigin.ESTIMATED),
        vram={},
        declared_artifact_size_bytes=10,
        kv_cache_bytes=10,
        requested_context_tokens=2,
        assumptions=(),
        confidence=0.9,
    )
    decision = ResourceManager.admit(
        snapshot=stale, estimate=estimate, policy=ResourcePolicy()
    )
    assert decision.status is AdmissionLevel.CONSTRAINED
    assert "resource_snapshot_stale" in decision.reasons

    gpu = GpuSample("gpu0", "GPU", 1000, 900, 10, 90.0, "measured")
    hot = make_snapshot(gpus=(gpu,))
    estimate = ResourceEstimate(
        estimate_id="e2", snapshot_id=hot.snapshot_id,
        ram=ResourceRange(1, 2, DataOrigin.ESTIMATED),
        vram={"gpu0": ResourceRange(1, 2, DataOrigin.ESTIMATED)},
        declared_artifact_size_bytes=1, kv_cache_bytes=1,
        requested_context_tokens=1, assumptions=(), confidence=1.0,
    )
    decision = ResourceManager.admit(
        snapshot=hot, estimate=estimate, policy=ResourcePolicy(max_temperature_c=85)
    )
    assert decision.status is AdmissionLevel.REJECTED
    assert decision.reasons == ("temperature_trip",)


@pytest.mark.asyncio
async def test_snapshot_measures_existing_parent_when_storage_path_missing(tmp_path):
    missing_path = tmp_path / "state" / "lar"

    manager = ResourceManager(storage_path=missing_path)
    snapshot = await manager.snapshot()

    assert snapshot.disk_total_bytes is not None
    assert snapshot.disk_free_bytes is not None
    assert snapshot.sources["storage"] == "statvfs"
    assert not missing_path.exists()
