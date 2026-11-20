from pathlib import Path

import pytest

from lar.evaluation.runner import BenchmarkRunner
from lar.evaluation.sandbox import SandboxError, SandboxResult
from lar.evaluation.specs import load_benchmark
from lar.providers.base import GenerationResponse, GenerationUsage
from lar.providers.ollama import OllamaConnectionError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = load_benchmark(
    PROJECT_ROOT / "benchmarks" / "coding" / "file_stats.yaml"
)

MODEL = "test-model:latest"

SUCCESS_OUTPUT = """
..........
Ran 10 tests in 0.010s

OK
"""


class FakeProvider:
    def __init__(self, response=None, error=None):
        self.response = response or {
            "response": "def analyze_files(files): return {}",
            "total_duration": 2_000_000_000,
            "load_duration": 500_000_000,
            "prompt_eval_count": 100,
            "prompt_eval_duration": 1_000_000_000,
            "eval_count": 100,
            "eval_duration": 2_000_000_000,
        }
        self.error = error
        self.generate_calls = []

    async def generate(self, **kwargs):
        self.generate_calls.append(kwargs)

        if self.error:
            raise self.error

        return GenerationResponse(
            text=self.response.get("response", ""),
            usage=GenerationUsage(
                total_duration_seconds=(
                    self.response.get("total_duration", 0) / 1_000_000_000
                ),
                load_duration_seconds=(
                    self.response.get("load_duration", 0) / 1_000_000_000
                ),
                input_tokens=self.response.get("prompt_eval_count"),
                input_duration_seconds=(
                    self.response.get("prompt_eval_duration", 0)
                    / 1_000_000_000
                ),
                output_tokens=self.response.get("eval_count"),
                output_duration_seconds=(
                    self.response.get("eval_duration", 0) / 1_000_000_000
                ),
            ),
            raw=self.response,
        )


class FakeSandbox:
    def __init__(self, result=None, error=None):
        self.result = result or SandboxResult(
            returncode=0,
            stdout=SUCCESS_OUTPUT,
            stderr="",
            duration_seconds=0.1,
        )
        self.error = error
        self.calls = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)

        if self.error:
            raise self.error

        return self.result


@pytest.fixture
def setup_runner():
    provider = FakeProvider()
    sandbox = FakeSandbox()
    runner = BenchmarkRunner(provider, sandbox)

    return runner, provider, sandbox


@pytest.mark.asyncio
async def test_runner_completes_successful_benchmark(setup_runner):
    runner, provider, sandbox = setup_runner

    result = await runner.run(MODEL, BENCHMARK)

    assert result.status == "passed"
    assert result.passed is True
    assert result.score == 1.0
    assert result.tests_run == 10
    assert result.tests_passed == 10
    assert result.metrics.generation_tokens_per_second == 50.0

    assert len(provider.generate_calls) == 1
    assert len(sandbox.calls) == 1
    assert sandbox.calls[0]["timeout_seconds"] == 10


@pytest.mark.asyncio
async def test_runner_extracts_markdown_code_fence(setup_runner):
    runner, provider, sandbox = setup_runner

    provider.response["response"] = (
        "```python\n"
        "def analyze_files(files):\n"
        "    return {}\n"
        "```"
    )

    result = await runner.run(MODEL, BENCHMARK)

    assert result.status == "passed"
    assert result.generated_code == (
        "def analyze_files(files):\n"
        "    return {}"
    )
    assert sandbox.calls[0]["solution_code"] == result.generated_code


@pytest.mark.asyncio
async def test_runner_calculates_partial_score(setup_runner):
    runner, _, sandbox = setup_runner

    sandbox.result = SandboxResult(
        returncode=1,
        stdout="",
        stderr=(
            "Ran 10 tests in 0.010s\n\n"
            "FAILED (failures=2, errors=1)\n"
        ),
        duration_seconds=0.1,
    )

    result = await runner.run(MODEL, BENCHMARK)

    assert result.status == "failed"
    assert result.passed is False
    assert result.tests_run == 10
    assert result.tests_passed == 7
    assert result.score == 0.7


@pytest.mark.asyncio
async def test_runner_reports_timeout(setup_runner):
    runner, _, sandbox = setup_runner

    sandbox.result = SandboxResult(
        returncode=-9,
        stdout="",
        stderr="",
        duration_seconds=10.0,
        timed_out=True,
    )

    result = await runner.run(MODEL, BENCHMARK)

    assert result.status == "timeout"
    assert result.score is None
    assert result.tests_run is None


@pytest.mark.asyncio
async def test_runner_reports_sandbox_launch_error(setup_runner):
    runner, _, sandbox = setup_runner

    sandbox.result = SandboxResult(
        returncode=None,
        stdout="",
        stderr="",
        duration_seconds=0.01,
        launch_error="bwrap non disponibile",
    )

    result = await runner.run(MODEL, BENCHMARK)

    assert result.status == "error"
    assert result.score is None
    assert "bwrap" in result.error


@pytest.mark.asyncio
async def test_runner_reports_provider_connection_error():
    provider = FakeProvider(
        error=OllamaConnectionError("Ollama non raggiungibile")
    )
    sandbox = FakeSandbox()
    runner = BenchmarkRunner(provider, sandbox)

    result = await runner.run(MODEL, BENCHMARK)

    assert result.status == "infrastructure_error"
    assert result.score is None
    assert result.sandbox_result is None
    assert "Ollama" in result.error
    assert sandbox.calls == []


@pytest.mark.asyncio
async def test_runner_reports_empty_model_response(setup_runner):
    runner, provider, sandbox = setup_runner

    provider.response["response"] = ""

    result = await runner.run(MODEL, BENCHMARK)

    assert result.status == "generation_error"
    assert result.score is None
    assert result.generated_code is None
    assert sandbox.calls == []


@pytest.mark.asyncio
async def test_runner_preserves_result_serialization(setup_runner):
    runner, _, _ = setup_runner

    result = await runner.run(MODEL, BENCHMARK)
    data = result.to_dict()

    assert data["benchmark_id"] == "coding.file_stats"
    assert data["benchmark_version"] == 2
    assert data["category"] == "coding"
    assert data["status"] == "passed"
    assert data["metrics"]["eval_count"] == 100
    assert data["sandbox_result"]["returncode"] == 0


@pytest.mark.asyncio
async def test_runner_propagates_sandbox_cleanup_error(setup_runner):
    runner, _, sandbox = setup_runner

    sandbox.error = SandboxError("Terminazione non confermata")

    with pytest.raises(SandboxError, match="Terminazione non confermata"):
        await runner.run(MODEL, BENCHMARK)
