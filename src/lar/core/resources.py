"""Host resource discovery and conservative admission without fabricated values."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil

from lar.providers.base import ProviderError


class DataOrigin(StrEnum):
    MEASURED = "measured"
    DECLARED = "declared"
    ESTIMATED = "estimated"
    SIMULATED = "simulated"
    UNAVAILABLE = "unavailable"


class AdmissionLevel(StrEnum):
    SAFE = "safe"
    CONSTRAINED = "constrained"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ProcessSample:
    pid: int
    name: str | None
    rss_bytes: int | None
    cpu_percent: float | None


@dataclass(frozen=True, slots=True)
class TemperatureSample:
    sensor: str
    label: str | None
    celsius: float
    source: str


@dataclass(frozen=True, slots=True)
class GpuSample:
    device_key: str
    name: str
    vram_total_bytes: int | None
    vram_free_bytes: int | None
    utilization_percent: float | None
    temperature_c: float | None
    source: str


@dataclass(frozen=True, slots=True)
class LoadedModelSample:
    model: str
    size_bytes: int | None
    size_vram_bytes: int | None
    context_length: int | None
    source: str


@dataclass(frozen=True, slots=True)
class HardwareProfile:
    fingerprint: str
    cpu_model: str | None
    logical_cores: int | None
    ram_total_bytes: int | None
    gpus: tuple[GpuSample, ...]
    discovered_at: datetime
    source: str


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    snapshot_id: str
    hardware_fingerprint: str
    sampled_at: datetime
    cpu_utilization_percent: float | None
    ram_total_bytes: int | None
    ram_available_bytes: int | None
    gpus: tuple[GpuSample, ...]
    temperatures: tuple[TemperatureSample, ...]
    external_processes: tuple[ProcessSample, ...] | None
    disk_total_bytes: int | None
    disk_free_bytes: int | None
    loaded_models: tuple[LoadedModelSample, ...]
    sources: dict[str, str]


@dataclass(frozen=True, slots=True)
class ResourceRange:
    lower_bytes: int | None
    upper_bytes: int | None
    origin: DataOrigin


@dataclass(frozen=True, slots=True)
class ResourceEstimate:
    estimate_id: str
    snapshot_id: str
    ram: ResourceRange
    vram: dict[str, ResourceRange]
    declared_artifact_size_bytes: int | None
    kv_cache_bytes: int | None
    requested_context_tokens: int | None
    assumptions: tuple[str, ...]
    confidence: float | None


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    status: AdmissionLevel
    can_execute: bool
    reasons: tuple[str, ...]
    ram_margin_bytes: int | None
    vram_margins_bytes: dict[str, int | None]
    policy_version: str


@dataclass(frozen=True, slots=True)
class ResourcePolicy:
    version: str = "host-dynamic-v1"
    ram_reserve_bytes: int = 0
    vram_reserve_bytes: int = 0
    disk_reserve_bytes: int = 0
    max_cpu_percent: float | None = None
    max_temperature_c: float | None = None
    max_snapshot_age_seconds: float = 30.0
    allow_constrained: bool = True

    def __post_init__(self) -> None:
        if min(self.ram_reserve_bytes, self.vram_reserve_bytes, self.disk_reserve_bytes) < 0:
            raise ValueError("Le riserve risorse non possono essere negative.")
        if self.max_snapshot_age_seconds <= 0:
            raise ValueError("La freschezza massima deve essere positiva.")

    @classmethod
    def from_config(cls, config: Any) -> ResourcePolicy:
        """Build host policy from validated RuntimeConfig without hardcoded host limits."""
        return cls(
            version=config.effective_hash(),
            ram_reserve_bytes=config.resource_ram_reserve_bytes,
            vram_reserve_bytes=config.resource_vram_reserve_bytes,
            disk_reserve_bytes=config.resource_disk_reserve_bytes,
            max_cpu_percent=config.resource_max_cpu_percent,
            max_temperature_c=config.resource_max_temperature_c,
            max_snapshot_age_seconds=config.resource_max_snapshot_age_seconds,
            allow_constrained=config.resource_allow_constrained,
        )


class ResourceManager:
    """Measure available host data; unsupported GPU/sensors remain unavailable."""

    def __init__(self, *, storage_path: Path | None = None, provider: Any = None) -> None:
        self.storage_path = Path(storage_path or Path.home())
        self.provider = provider
        self._hardware: HardwareProfile | None = None

    async def discover(self) -> HardwareProfile:
        memory = psutil.virtual_memory()
        cpu_model = None
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.lower().startswith(("model name", "hardware")) and ":" in line:
                    cpu_model = line.split(":", 1)[1].strip()
                    break
        except OSError:
            pass
        gpus = await self._discover_gpus()
        identity = f"{cpu_model}|{psutil.cpu_count(logical=True)}|{memory.total}|" + "|".join(
            gpu.name for gpu in gpus
        )
        fingerprint = hashlib.sha256(identity.encode()).hexdigest()
        self._hardware = HardwareProfile(
            fingerprint=fingerprint,
            cpu_model=cpu_model,
            logical_cores=psutil.cpu_count(logical=True),
            ram_total_bytes=memory.total if memory.total > 0 else None,
            gpus=gpus,
            discovered_at=datetime.now(UTC),
            source="psutil+/proc; GPU=nvidia-smi when available",
        )
        return self._hardware

    async def snapshot(self) -> ResourceSnapshot:
        hardware = self._hardware or await self.discover()
        memory = psutil.virtual_memory()
        try:
            cpu = psutil.cpu_percent(interval=0.05)
        except (OSError, RuntimeError):
            cpu = None
        gpus = await self._discover_gpus()
        temperatures = self._temperatures()
        processes = self._processes()
        loaded_models = await self._loaded_models()
        try:
            disk_path = self.storage_path
            while not disk_path.exists() and disk_path != disk_path.parent:
                disk_path = disk_path.parent
            disk = shutil.disk_usage(disk_path)
            disk_total, disk_free = disk.total, disk.free
        except OSError:
            disk_total = disk_free = None
        sources = {
            "cpu": "psutil" if cpu is not None else "unavailable",
            "ram": "psutil" if memory.available is not None else "unavailable",
            "gpu": "nvidia-smi" if gpus else "unavailable",
            "temperature": "psutil sensors" if temperatures else "unavailable",
            "processes": "psutil" if processes is not None else "unavailable",
            "storage": "statvfs" if disk_free is not None else "unavailable",
            "loaded_models": "ollama:/api/ps" if loaded_models is not None else "unavailable",
        }
        return ResourceSnapshot(
            snapshot_id=str(uuid4()),
            hardware_fingerprint=hardware.fingerprint,
            sampled_at=datetime.now(UTC),
            cpu_utilization_percent=cpu,
            ram_total_bytes=memory.total if memory.total > 0 else None,
            ram_available_bytes=memory.available if memory.available >= 0 else None,
            gpus=gpus,
            temperatures=temperatures,
            external_processes=processes,
            disk_total_bytes=disk_total,
            disk_free_bytes=disk_free,
            loaded_models=loaded_models or (),
            sources=sources,
        )

    async def _discover_gpus(self) -> tuple[GpuSample, ...]:
        executable = shutil.which("nvidia-smi")
        if executable is None:
            return ()
        command = [
            executable,
            "--query-gpu=index,name,memory.total,memory.free,utilization.gpu,temperature.gpu",
            "--format=csv,noheader,nounits",
        ]
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=2.0)
        except (OSError, TimeoutError, subprocess.SubprocessError):
            if process is not None and process.returncode is None:
                process.kill()
                await process.communicate()
            return ()
        if process.returncode != 0:
            return ()
        samples = []
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            fields = [field.strip() for field in line.split(",", 5)]
            if len(fields) != 6:
                continue
            index, name, total, free, utilization, temperature = fields
            samples.append(
                GpuSample(
                    device_key=f"nvidia:{index}",
                    name=name,
                    vram_total_bytes=self._parse_mib(total),
                    vram_free_bytes=self._parse_mib(free),
                    utilization_percent=self._parse_float(utilization),
                    temperature_c=self._parse_float(temperature),
                    source="nvidia-smi:measured",
                )
            )
        return tuple(samples)

    @staticmethod
    def _parse_mib(value: str) -> int | None:
        parsed = ResourceManager._parse_float(value)
        return int(parsed * 1024 * 1024) if parsed is not None and parsed >= 0 else None

    @staticmethod
    def _parse_float(value: str) -> float | None:
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None

    @staticmethod
    def _temperatures() -> tuple[TemperatureSample, ...]:
        try:
            readings = psutil.sensors_temperatures(fahrenheit=False)
        except (AttributeError, NotImplementedError, OSError):
            return ()
        return tuple(
            TemperatureSample(sensor=name, label=item.label or None, celsius=item.current, source="psutil:measured")
            for name, group in readings.items()
            for item in group
            if item.current is not None
        )

    @staticmethod
    def _processes() -> tuple[ProcessSample, ...] | None:
        try:
            processes = []
            own_pid = os.getpid()
            for process in psutil.process_iter(("pid", "name", "memory_info", "cpu_percent")):
                try:
                    values = process.info
                    if values.get("pid") == own_pid:
                        continue
                    memory = values.get("memory_info")
                    rss = memory.rss if memory is not None and memory.rss >= 0 else None
                    processes.append(
                        ProcessSample(
                            pid=int(values["pid"]),
                            name=values.get("name"),
                            rss_bytes=rss,
                            cpu_percent=values.get("cpu_percent"),
                        )
                    )
                except (psutil.Error, KeyError, TypeError, ValueError):
                    continue
            return tuple(processes)
        except (psutil.Error, OSError):
            return None

    async def _loaded_models(self) -> tuple[LoadedModelSample, ...] | None:
        if self.provider is None:
            return None
        try:
            running = await self.provider.list_running()
        except (ProviderError, OSError, TypeError):
            return None
        samples = []
        for item in running:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            samples.append(
                LoadedModelSample(
                    model=item["name"],
                    size_bytes=self._valid_int(item.get("size")),
                    size_vram_bytes=self._valid_int(item.get("size_vram")),
                    context_length=self._valid_int(item.get("context_length")),
                    source="ollama:/api/ps:measured",
                )
            )
        return tuple(samples)

    @staticmethod
    def _valid_int(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    @staticmethod
    def estimate(
        *,
        snapshot: ResourceSnapshot,
        artifact_size_bytes: int | None,
        model_info: dict[str, Any] | None = None,
        requested_context_tokens: int | None = None,
        sequences: int = 1,
    ) -> ResourceEstimate:
        """Estimate only when all inputs to an equation are declared and known."""
        info = model_info or {}
        assumptions: list[str] = ["Runtime allocation is not guaranteed by artifact size."]
        kv_cache = None
        layers = ResourceManager._metadata_int(info, "llama.block_count")
        kv_heads = ResourceManager._metadata_int(info, "llama.attention.head_count_kv")
        head_dim = ResourceManager._metadata_int(info, "lar.head_dimension")
        cache_bytes = ResourceManager._metadata_int(info, "lar.kv_cache_bytes_per_element")
        if (
            requested_context_tokens is not None
            and requested_context_tokens >= 0
            and sequences > 0
            and all(value is not None and value > 0 for value in (layers, kv_heads, head_dim, cache_bytes))
        ):
            kv_cache = 2 * layers * kv_heads * head_dim * cache_bytes * requested_context_tokens * sequences
            assumptions.append("KV cache formula: 2 × layers × KV heads × head dimension × bytes × context × sequences.")
        else:
            assumptions.append("KV cache unknown: model architecture/cache precision or context metadata missing.")
        ram_lower = ResourceManager._metadata_int(info, "lar.estimated_ram_lower_bytes")
        ram_upper = ResourceManager._metadata_int(info, "lar.estimated_ram_upper_bytes")
        if ram_lower is not None and ram_upper is not None and ram_lower > ram_upper:
            ram_lower = ram_upper = None
            assumptions.append("Invalid RAM estimates ignored.")
        vram_lower = ResourceManager._metadata_int(info, "lar.estimated_vram_lower_bytes")
        vram_upper = ResourceManager._metadata_int(info, "lar.estimated_vram_upper_bytes")
        if vram_lower is not None and vram_upper is not None and vram_lower > vram_upper:
            vram_lower = vram_upper = None
            assumptions.append("Invalid VRAM estimates ignored.")
        confidence = info.get("lar.resource_estimate_confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            confidence = None
        return ResourceEstimate(
            estimate_id=str(uuid4()),
            snapshot_id=snapshot.snapshot_id,
            ram=ResourceRange(ram_lower, ram_upper, DataOrigin.ESTIMATED),
            vram={
                gpu.device_key: ResourceRange(vram_lower, vram_upper, DataOrigin.ESTIMATED)
                for gpu in snapshot.gpus
            },
            declared_artifact_size_bytes=(
                artifact_size_bytes if artifact_size_bytes is not None and artifact_size_bytes > 0 else None
            ),
            kv_cache_bytes=kv_cache,
            requested_context_tokens=requested_context_tokens,
            assumptions=tuple(assumptions),
            confidence=float(confidence) if confidence is not None else (1.0 if kv_cache is not None else None),
        )

    @staticmethod
    def _metadata_int(metadata: dict[str, Any], key: str) -> int | None:
        value = metadata.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    @staticmethod
    def admit(
        *, snapshot: ResourceSnapshot, estimate: ResourceEstimate, policy: ResourcePolicy
    ) -> AdmissionDecision:
        reasons: list[str] = []
        now = datetime.now(UTC)
        age = max(0.0, (now - snapshot.sampled_at).total_seconds())
        stale = age > policy.max_snapshot_age_seconds
        ram_budget = (
            snapshot.ram_available_bytes - policy.ram_reserve_bytes
            if snapshot.ram_available_bytes is not None
            else None
        )
        ram_margin = (
            ram_budget - estimate.ram.upper_bytes
            if ram_budget is not None and estimate.ram.upper_bytes is not None
            else None
        )
        if (
            ram_budget is not None
            and estimate.ram.lower_bytes is not None
            and estimate.ram.lower_bytes > ram_budget
        ):
            return AdmissionDecision(
                AdmissionLevel.REJECTED, False, ("ram_lower_bound_exceeds_available",),
                ram_margin, {}, policy.version
            )
        if stale:
            reasons.append("resource_snapshot_stale")
        if snapshot.ram_available_bytes is None:
            reasons.append("ram_measurement_unavailable")
        if estimate.ram.upper_bytes is None:
            reasons.append("ram_upper_bound_unknown")
        elif ram_budget is None:
            reasons.append("ram_budget_unavailable")
        elif estimate.ram.upper_bytes > ram_budget:
            reasons.append("ram_upper_bound_exceeds_budget")
        vram_margins: dict[str, int | None] = {}
        for gpu in snapshot.gpus:
            budget = (
                gpu.vram_free_bytes - policy.vram_reserve_bytes
                if gpu.vram_free_bytes is not None
                else None
            )
            estimate_range = estimate.vram.get(gpu.device_key)
            lower = estimate_range.lower_bytes if estimate_range else None
            upper = estimate_range.upper_bytes if estimate_range else None
            margin = budget - upper if budget is not None and upper is not None else None
            vram_margins[gpu.device_key] = margin
            if budget is None:
                reasons.append(f"vram_budget_unavailable:{gpu.device_key}")
            elif lower is not None and lower > budget:
                return AdmissionDecision(
                    AdmissionLevel.REJECTED,
                    False,
                    (f"vram_lower_bound_exceeds_budget:{gpu.device_key}",),
                    ram_margin,
                    vram_margins,
                    policy.version,
                )
            elif upper is None:
                reasons.append(f"vram_upper_bound_unknown:{gpu.device_key}")
            elif upper > budget:
                reasons.append(f"vram_upper_bound_exceeds_budget:{gpu.device_key}")
        if estimate.kv_cache_bytes is None:
            reasons.append("kv_cache_unknown")
        if policy.max_cpu_percent is not None:
            if snapshot.cpu_utilization_percent is None:
                reasons.append("cpu_measurement_unavailable")
            elif snapshot.cpu_utilization_percent > policy.max_cpu_percent:
                reasons.append("cpu_pressure")
        if policy.max_temperature_c is not None:
            temps = [sample.celsius for sample in snapshot.temperatures]
            temps.extend(gpu.temperature_c for gpu in snapshot.gpus if gpu.temperature_c is not None)
            if not temps:
                reasons.append("temperature_unavailable")
            elif max(temps) >= policy.max_temperature_c:
                return AdmissionDecision(
                    AdmissionLevel.REJECTED, False, ("temperature_trip",),
                    ram_margin, {}, policy.version
                )
        if policy.disk_reserve_bytes and snapshot.disk_free_bytes is None:
            reasons.append("storage_measurement_unavailable")
        elif (
            snapshot.disk_free_bytes is not None
            and snapshot.disk_free_bytes < policy.disk_reserve_bytes
        ):
            return AdmissionDecision(
                AdmissionLevel.REJECTED, False, ("storage_reserve_below_minimum",),
                ram_margin, {}, policy.version
            )
        if not reasons and estimate.confidence is not None:
            return AdmissionDecision(
                AdmissionLevel.SAFE,
                True,
                (),
                ram_margin,
                vram_margins,
                policy.version,
            )
        can_execute = policy.allow_constrained
        status = AdmissionLevel.CONSTRAINED if can_execute else AdmissionLevel.REJECTED
        if not can_execute:
            reasons.append("uncertainty_policy_rejects_constrained")
        return AdmissionDecision(
            status, can_execute, tuple(reasons), ram_margin, vram_margins, policy.version
        )
