"""Deterministic task analysis and auditable resource-aware model selection."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import uuid4

from lar.core.resources import (
    AdmissionDecision,
    AdmissionLevel,
    ResourceEstimate,
    ResourceManager,
    ResourcePolicy,
    ResourceSnapshot,
)


class CandidateStatus(StrEnum):
    ELIGIBLE = "eligible"
    UNSCORED = "unscored"
    CONSTRAINED = "constrained"
    REJECTED = "rejected"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True, slots=True)
class TaskProfile:
    analyzer_version: str
    task_tags: frozenset[str]
    requires_tools: bool
    requires_vision: bool
    requires_json: bool
    input_tokens_estimate: int | None
    max_output_tokens: int | None
    required_context_tokens: int | None
    context_estimation_method: str


class TaskAnalyzer:
    """Extract request requirements without invoking any language model."""

    version = "task-analyzer-v1"

    @staticmethod
    def analyze(request: Mapping[str, Any]) -> TaskProfile:
        messages = request.get("messages")
        requires_vision = False
        if isinstance(messages, list):
            for message in messages:
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, list) and any(
                    isinstance(part, dict)
                    and part.get("type") in {"image_url", "input_image"}
                    for part in content
                ):
                    requires_vision = True
        tools = request.get("tools")
        requires_tools = isinstance(tools, list) and len(tools) > 0
        response_format = request.get("response_format")
        requires_json = isinstance(response_format, dict) and response_format.get("type") in {
            "json_object",
            "json_schema",
        }
        output_tokens = request.get("max_tokens", request.get("max_completion_tokens"))
        if isinstance(output_tokens, bool) or not isinstance(output_tokens, int) or output_tokens <= 0:
            output_tokens = None
        # No tokenizer is presumed compatible across model digests/templates.
        input_tokens = None
        tags = set()
        if requires_tools:
            tags.add("tool_calling")
        if requires_vision:
            tags.add("vision")
        if requires_json:
            tags.add("json")
        if not tags:
            tags.add("chat")
        return TaskProfile(
            analyzer_version=TaskAnalyzer.version,
            task_tags=frozenset(tags),
            requires_tools=requires_tools,
            requires_vision=requires_vision,
            requires_json=requires_json,
            input_tokens_estimate=input_tokens,
            max_output_tokens=output_tokens,
            required_context_tokens=(output_tokens if input_tokens is None else input_tokens + (output_tokens or 0)),
            context_estimation_method="unavailable:model-compatible-tokenizer-not-configured",
        )


@dataclass(frozen=True, slots=True)
class BenchmarkMetric:
    value: float | None
    direction: str
    unit: str
    origin: str = "measured"


@dataclass(frozen=True, slots=True)
class BenchmarkEvidence:
    benchmark_id: str
    benchmark_version: str
    spec_digest: str | None
    artifact_digest: str | None
    quantization: str | None
    hardware_fingerprint: str | None
    runtime_version: str | None
    options_digest: str | None
    status: str
    sample_count: int
    metrics: Mapping[str, BenchmarkMetric]

    @property
    def comparison_key(self) -> tuple[str, ...] | None:
        required = (
            self.spec_digest,
            self.artifact_digest,
            self.quantization,
            self.hardware_fingerprint,
            self.runtime_version,
            self.options_digest,
        )
        if any(not value for value in required):
            return None
        return (
            self.benchmark_id,
            self.benchmark_version,
            self.spec_digest or "",
            self.quantization or "",
            self.hardware_fingerprint or "",
            self.runtime_version or "",
            self.options_digest or "",
        )


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    model_id: str
    digest: str | None
    family: str | None
    capabilities: frozenset[str] | None
    context_limit_tokens: int | None
    artifact_size_bytes: int | None
    model_info: Mapping[str, Any]
    benchmarks: tuple[BenchmarkEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateTrace:
    model_id: str
    status: CandidateStatus
    reasons: tuple[str, ...]
    estimate: ResourceEstimate | None
    admission: AdmissionDecision | None
    benchmark_state: str
    pareto_front: int | None
    evidence_coverage: float


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    decision_id: str
    request_id: str
    selected_model_id: str | None
    alternatives: tuple[str, ...]
    task: TaskProfile
    snapshot_id: str
    policy_version: str
    candidate_traces: tuple[CandidateTrace, ...]
    status: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoutingConstraints:
    excluded_model_ids: frozenset[str] = frozenset()
    preferred_families: tuple[str, ...] = ()


class ModelRouter:
    """Apply hard compatibility/admission filters, then benchmark Pareto ordering."""

    version = "model-router-v1"

    def __init__(
        self,
        *,
        resource_policy: ResourcePolicy,
        memory_estimator: Callable[..., ResourceEstimate] = ResourceManager.estimate,
    ) -> None:
        self.resource_policy = resource_policy
        self.memory_estimator = memory_estimator

    def route(
        self,
        *,
        request_id: str,
        request: Mapping[str, Any],
        candidates: Sequence[ModelCandidate],
        snapshot: ResourceSnapshot,
        constraints: RoutingConstraints = RoutingConstraints(),
    ) -> RoutingDecision:
        task = TaskAnalyzer.analyze(request)
        traces: list[CandidateTrace] = []
        eligible: list[tuple[ModelCandidate, CandidateTrace]] = []
        for candidate in sorted(candidates, key=lambda item: (item.model_id, item.digest or "")):
            if candidate.model_id in constraints.excluded_model_ids:
                traces.append(self._trace(candidate, CandidateStatus.REJECTED, ("excluded_by_policy",)))
                continue
            incompatible = self._compatibility(candidate, task)
            if incompatible:
                traces.append(self._trace(candidate, CandidateStatus.INCOMPATIBLE, incompatible))
                continue
            if (
                task.required_context_tokens is not None
                and candidate.context_limit_tokens is not None
                and task.required_context_tokens > candidate.context_limit_tokens
            ):
                traces.append(
                    self._trace(candidate, CandidateStatus.INCOMPATIBLE, ("context_limit_exceeded",))
                )
                continue
            estimate = self.memory_estimator(
                snapshot=snapshot,
                artifact_size_bytes=candidate.artifact_size_bytes,
                model_info=dict(candidate.model_info),
                requested_context_tokens=task.required_context_tokens,
            )
            admission = ResourceManager.admit(
                snapshot=snapshot, estimate=estimate, policy=self.resource_policy
            )
            if admission.status is AdmissionLevel.REJECTED:
                traces.append(
                    self._trace(candidate, CandidateStatus.REJECTED, admission.reasons, estimate, admission)
                )
                continue
            reasons = list(admission.reasons)
            if task.input_tokens_estimate is None:
                reasons.append("input_context_estimate_unavailable")
            status = (
                CandidateStatus.CONSTRAINED
                if admission.status is AdmissionLevel.CONSTRAINED or reasons
                else CandidateStatus.ELIGIBLE
            )
            trace = self._trace(candidate, status, tuple(reasons), estimate, admission)
            traces.append(trace)
            eligible.append((candidate, trace))

        if not eligible:
            return RoutingDecision(
                str(uuid4()), request_id, None, (), task, snapshot.snapshot_id,
                self.resource_policy.version, tuple(traces), "rejected", ("no_eligible_model",)
            )

        comparable_key, evidence_by_model = self._select_comparable_group(eligible, snapshot)
        fronts: dict[str, int] = {}
        if comparable_key is not None:
            remaining = set(evidence_by_model)
            front = 1
            while remaining:
                current = {
                    model_id
                    for model_id in remaining
                    if not any(
                        self._dominates(evidence_by_model[other], evidence_by_model[model_id])
                        for other in remaining
                        if other != model_id
                    )
                }
                for model_id in current:
                    fronts[model_id] = front
                remaining -= current
                front += 1

        preference_rank = {family: index for index, family in enumerate(constraints.preferred_families)}

        def ordering(item: tuple[ModelCandidate, CandidateTrace]) -> tuple[Any, ...]:
            candidate, _ = item
            front = fronts.get(candidate.model_id)
            benchmark_order = (0, front) if front is not None else (1, 0)
            preferred = preference_rank.get(candidate.family or "", len(preference_rank))
            return (*benchmark_order, preferred, candidate.model_id, candidate.digest or "")

        ranked = sorted(eligible, key=ordering)
        selected = ranked[0][0]
        updated = []
        for trace in traces:
            evidence = evidence_by_model.get(trace.model_id)
            if trace.status in {CandidateStatus.ELIGIBLE, CandidateStatus.CONSTRAINED}:
                if evidence is None:
                    trace = CandidateTrace(
                        trace.model_id, CandidateStatus.UNSCORED,
                        (*trace.reasons, "benchmark_unscored_or_not_comparable"),
                        trace.estimate, trace.admission, "unscored", None, 0.0,
                    )
                else:
                    trace = CandidateTrace(
                        trace.model_id, trace.status, trace.reasons, trace.estimate,
                        trace.admission, "comparable", fronts.get(trace.model_id), 1.0,
                    )
            updated.append(trace)
        return RoutingDecision(
            str(uuid4()), request_id, selected.model_id,
            tuple(candidate.model_id for candidate, _ in ranked[1:]), task,
            snapshot.snapshot_id, self.resource_policy.version, tuple(updated),
            "selected", ("pareto_benchmark" if selected.model_id in fronts else "unscored_stable_tiebreak",),
        )

    @staticmethod
    def _compatibility(candidate: ModelCandidate, task: TaskProfile) -> tuple[str, ...]:
        required = set()
        if task.requires_tools:
            required.add("tools")
        if task.requires_vision:
            required.add("vision")
        if task.requires_json:
            required.add("json")
        if not required:
            return ()
        if candidate.capabilities is None:
            return ("capabilities_unknown",)
        aliases = {"tool_calling": "tools", "function_calling": "tools", "json_mode": "json"}
        supported = {aliases.get(value, value) for value in candidate.capabilities}
        missing = sorted(required - supported)
        return tuple(f"capability_missing:{item}" for item in missing)

    @staticmethod
    def _trace(
        candidate: ModelCandidate,
        status: CandidateStatus,
        reasons: tuple[str, ...],
        estimate: ResourceEstimate | None = None,
        admission: AdmissionDecision | None = None,
    ) -> CandidateTrace:
        return CandidateTrace(candidate.model_id, status, reasons, estimate, admission, "unscored", None, 0.0)

    @staticmethod
    def _select_comparable_group(
        eligible: Sequence[tuple[ModelCandidate, CandidateTrace]], snapshot: ResourceSnapshot
    ) -> tuple[tuple[str, ...] | None, dict[str, BenchmarkEvidence]]:
        groups: dict[tuple[str, ...], dict[str, BenchmarkEvidence]] = {}
        for candidate, _trace in eligible:
            for evidence in candidate.benchmarks:
                key = evidence.comparison_key
                if (
                    evidence.status != "comparable"
                    or evidence.sample_count < 1
                    or key is None
                    or evidence.artifact_digest != candidate.digest
                    or evidence.hardware_fingerprint != snapshot.hardware_fingerprint
                    or not evidence.metrics
                    or any(
                        metric.value is None
                        or metric.origin != "measured"
                        or metric.direction not in {"maximize", "minimize"}
                        for metric in evidence.metrics.values()
                    )
                ):
                    continue
                groups.setdefault(key, {})[candidate.model_id] = evidence
        viable = [(key, values) for key, values in groups.items() if len(values) >= 2]
        if not viable:
            return None, {}
        key, values = min(viable, key=lambda item: (-len(item[1]), item[0]))
        shared_metrics = set.intersection(*(set(value.metrics) for value in values.values()))
        if not shared_metrics:
            return None, {}
        normalized = {
            model_id: BenchmarkEvidence(
                evidence.benchmark_id,
                evidence.benchmark_version,
                evidence.spec_digest,
                evidence.artifact_digest,
                evidence.quantization,
                evidence.hardware_fingerprint,
                evidence.runtime_version,
                evidence.options_digest,
                evidence.status,
                evidence.sample_count,
                {name: evidence.metrics[name] for name in sorted(shared_metrics)},
            )
            for model_id, evidence in values.items()
        }
        return key, normalized

    @staticmethod
    def _dominates(left: BenchmarkEvidence, right: BenchmarkEvidence) -> bool:
        if set(left.metrics) != set(right.metrics):
            return False
        better = False
        for name in left.metrics:
            left_metric = left.metrics[name]
            right_metric = right.metrics[name]
            if left_metric.direction != right_metric.direction or left_metric.unit != right_metric.unit:
                return False
            left_value = left_metric.value
            right_value = right_metric.value
            if left_value is None or right_value is None:
                return False
            if left_metric.direction == "maximize":
                if left_value < right_value:
                    return False
                better |= left_value > right_value
            else:
                if left_value > right_value:
                    return False
                better |= left_value < right_value
        return better
