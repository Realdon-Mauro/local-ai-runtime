from datetime import UTC, datetime

from lar.core.resources import ResourcePolicy, ResourceSnapshot
from lar.core.router import (
    BenchmarkEvidence,
    BenchmarkMetric,
    CandidateStatus,
    ModelCandidate,
    ModelRouter,
    RoutingConstraints,
    TaskAnalyzer,
)


def snapshot():
    return ResourceSnapshot(
        snapshot_id="snapshot-1",
        hardware_fingerprint="host-a",
        sampled_at=datetime.now(UTC),
        cpu_utilization_percent=5,
        ram_total_bytes=10_000,
        ram_available_bytes=8_000,
        gpus=(),
        temperatures=(),
        external_processes=(),
        disk_total_bytes=20_000,
        disk_free_bytes=10_000,
        loaded_models=(),
        sources={"ram": "measured"},
    )


def model(model_id, *, capabilities=frozenset(), context=4096, family="f", benchmarks=()):
    return ModelCandidate(
        model_id=model_id,
        digest=f"digest-{model_id}",
        family=family,
        capabilities=capabilities,
        context_limit_tokens=context,
        artifact_size_bytes=100,
        model_info={
            "llama.block_count": 1,
            "llama.attention.head_count_kv": 1,
            "lar.head_dimension": 1,
            "lar.kv_cache_bytes_per_element": 1,
            "lar.estimated_ram_lower_bytes": 100,
            "lar.estimated_ram_upper_bytes": 200,
            "lar.resource_estimate_confidence": 0.9,
        },
        benchmarks=tuple(benchmarks),
    )


def evidence(model_id, quality, throughput, *, hardware="host-a", status="comparable"):
    return BenchmarkEvidence(
        benchmark_id="code-basic",
        benchmark_version="1",
        spec_digest="spec-hash",
        artifact_digest=f"digest-{model_id}",
        quantization="Q4_K_M",
        hardware_fingerprint=hardware,
        runtime_version="ollama-1.0",
        options_digest="options-hash",
        status=status,
        sample_count=3,
        metrics={
            "quality": BenchmarkMetric(quality, "maximize", "score"),
            "tokens_per_second": BenchmarkMetric(throughput, "maximize", "tokens/s"),
        },
    )


def router():
    return ModelRouter(resource_policy=ResourcePolicy())


def test_analyzer_is_deterministic_and_does_not_guess_input_tokens():
    request = {
        "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": "x"}]}],
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
        "response_format": {"type": "json_object"},
        "max_tokens": 500,
    }
    first = TaskAnalyzer.analyze(request)
    second = TaskAnalyzer.analyze(request)
    assert first == second
    assert first.requires_tools and first.requires_vision and first.requires_json
    assert first.input_tokens_estimate is None
    assert first.required_context_tokens == 500


def test_router_filters_hard_capability_and_context_mismatches():
    decision = router().route(
        request_id="r1",
        request={"messages": [{"role": "user", "content": "hello"}], "tools": [{"type": "function"}], "max_tokens": 10},
        candidates=[model("no-tools", context=100), model("tools", capabilities=frozenset({"tools"}), context=5)],
        snapshot=snapshot(),
    )
    assert decision.selected_model_id is None
    traces = {trace.model_id: trace for trace in decision.candidate_traces}
    assert traces["no-tools"].status is CandidateStatus.INCOMPATIBLE
    assert traces["no-tools"].reasons == ("capability_missing:tools",)
    assert traces["tools"].reasons == ("context_limit_exceeded",)


def test_unscored_candidates_use_stable_tiebreak_and_family_preference():
    decision = router().route(
        request_id="r2",
        request={"messages": [{"role": "user", "content": "hello"}]},
        candidates=[model("zeta", family="other"), model("beta", family="preferred"), model("alpha")],
        snapshot=snapshot(),
        constraints=RoutingConstraints(preferred_families=("preferred",)),
    )
    assert decision.selected_model_id == "beta"
    assert decision.alternatives == ("alpha", "zeta")
    assert all(trace.status is CandidateStatus.UNSCORED for trace in decision.candidate_traces)
    assert all(trace.benchmark_state == "unscored" for trace in decision.candidate_traces)


def test_pareto_benchmark_orders_only_same_comparable_group():
    candidates = [
        model("a", benchmarks=[evidence("a", 0.9, 40)]),
        model("b", benchmarks=[evidence("b", 0.7, 20)]),
        model("c", benchmarks=[evidence("c", 0.8, 30)]),
    ]
    decision = router().route(
        request_id="r3",
        request={"messages": [{"role": "user", "content": "hello"}]},
        candidates=candidates,
        snapshot=snapshot(),
    )
    assert decision.selected_model_id == "a"
    traces = {trace.model_id: trace for trace in decision.candidate_traces}
    assert traces["a"].pareto_front == 1
    assert traces["c"].pareto_front == 2
    assert traces["b"].pareto_front == 3
    assert traces["a"].benchmark_state == "comparable"


def test_cross_hardware_benchmark_is_not_used_to_rank():
    decision = router().route(
        request_id="r4",
        request={"messages": [{"role": "user", "content": "hello"}]},
        candidates=[
            model("a", benchmarks=[evidence("a", 0.9, 50, hardware="other-host")]),
            model("b", benchmarks=[evidence("b", 0.1, 1, hardware="other-host")]),
        ],
        snapshot=snapshot(),
    )
    assert decision.selected_model_id == "a"
    assert decision.reasons == ("unscored_stable_tiebreak",)
    assert all(trace.benchmark_state == "unscored" for trace in decision.candidate_traces)


def test_excluded_candidates_are_not_eligible_or_fallbacks():
    decision = router().route(
        request_id="r5",
        request={"messages": [{"role": "user", "content": "hello"}]},
        candidates=[model("a"), model("b")],
        snapshot=snapshot(),
        constraints=RoutingConstraints(excluded_model_ids=frozenset({"a", "b"})),
    )
    assert decision.selected_model_id is None
    assert decision.alternatives == ()
    assert all(trace.reasons == ("excluded_by_policy",) for trace in decision.candidate_traces)
