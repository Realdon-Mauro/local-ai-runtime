import re
from dataclasses import asdict, dataclass
from typing import Any

from lar.evaluation.metrics import GenerationMetrics
from lar.evaluation.sandbox import SandboxExecutor, SandboxResult
from lar.evaluation.specs import BenchmarkSpec
from lar.providers.base import GenerationProvider, ProviderError


@dataclass(frozen=True)
class BenchmarkResult:
    """Risultato completo di una singola esecuzione benchmark."""

    model: str
    benchmark_id: str
    benchmark_version: int
    category: str
    status: str
    score: float | None
    tests_run: int | None
    tests_passed: int | None
    generated_code: str | None
    generation_response: dict[str, Any]
    metrics: GenerationMetrics
    sandbox_result: SandboxResult | None
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "benchmark_id": self.benchmark_id,
            "benchmark_version": self.benchmark_version,
            "category": self.category,
            "status": self.status,
            "score": self.score,
            "tests_run": self.tests_run,
            "tests_passed": self.tests_passed,
            "generated_code": self.generated_code,
            "generation_response": self.generation_response,
            "metrics": self.metrics.to_dict(),
            "sandbox_result": (
                asdict(self.sandbox_result)
                if self.sandbox_result is not None
                else None
            ),
            "error": self.error,
        }


class BenchmarkRunner:
    """Coordina generazione, valutazione e raccolta delle metriche."""

    def __init__(
        self,
        provider: GenerationProvider,
        sandbox: SandboxExecutor | None = None,
    ) -> None:
        self.provider = provider
        self.sandbox = sandbox or SandboxExecutor()

    @staticmethod
    def _extract_code(response: str) -> str:
        """Rimuove eventuali fence Markdown che racchiudono tutto il codice."""

        content = response.strip()

        match = re.fullmatch(
            r"```(?:python|py)?[ \t]*\r?\n(.*?)\r?\n```",
            content,
            re.DOTALL | re.IGNORECASE,
        )

        if match:
            return match.group(1).strip()

        return content

    @staticmethod
    def _parse_test_summary(
        output: str,
    ) -> tuple[int | None, int | None]:
        """Estrae il numero di test eseguiti e superati da unittest."""

        run_match = re.search(
            r"^Ran\s+(\d+)\s+tests?\s+in\s+.+$",
            output,
            re.MULTILINE,
        )

        if not run_match:
            return None, None

        tests_run = int(run_match.group(1))

        summary_match = re.search(
            r"^(?:OK|FAILED)(?:\s*\((.*?)\))?\s*$",
            output,
            re.MULTILINE,
        )

        if not summary_match:
            return tests_run, None

        summary = summary_match.group(1) or ""
        counts = {
            name: int(value)
            for name, value in re.findall(
                r"(failures|errors|skipped)=(\d+)",
                summary,
            )
        }

        tests_passed = max(
            0,
            tests_run
            - counts.get("failures", 0)
            - counts.get("errors", 0)
            - counts.get("skipped", 0),
        )

        return tests_run, tests_passed

    @staticmethod
    def _empty_metrics() -> GenerationMetrics:
        return GenerationMetrics(
            total_duration_ns=None,
            load_duration_ns=None,
            prompt_eval_count=None,
            prompt_eval_duration_ns=None,
            eval_count=None,
            eval_duration_ns=None,
        )

    async def run(
        self,
        model: str,
        benchmark: BenchmarkSpec,
    ) -> BenchmarkResult:
        """Esegue un benchmark completo su un singolo modello."""

        if not isinstance(model, str) or not model.strip():
            raise ValueError("Il nome del modello non può essere vuoto.")

        response: dict[str, Any] = {}

        try:
            generated_response = await self.provider.generate(
                model=model,
                prompt=benchmark.prompt,
                options=benchmark.generation,
            )

        except ProviderError as exc:
            return BenchmarkResult(
                model=model,
                benchmark_id=benchmark.id,
                benchmark_version=benchmark.version,
                category=benchmark.category,
                status="infrastructure_error",
                score=None,
                tests_run=None,
                tests_passed=None,
                generated_code=None,
                generation_response=response,
                metrics=self._empty_metrics(),
                sandbox_result=None,
                error=str(exc),
            )

        response = generated_response.raw
        metrics = GenerationMetrics.from_usage(generated_response.usage)
        generated_text = generated_response.text

        if not isinstance(generated_text, str) or not generated_text.strip():
            return BenchmarkResult(
                model=model,
                benchmark_id=benchmark.id,
                benchmark_version=benchmark.version,
                category=benchmark.category,
                status="generation_error",
                score=None,
                tests_run=None,
                tests_passed=None,
                generated_code=None,
                generation_response=response,
                metrics=metrics,
                sandbox_result=None,
                error="Il modello non ha restituito codice.",
            )

        generated_code = self._extract_code(generated_text)

        if not generated_code:
            return BenchmarkResult(
                model=model,
                benchmark_id=benchmark.id,
                benchmark_version=benchmark.version,
                category=benchmark.category,
                status="generation_error",
                score=None,
                tests_run=None,
                tests_passed=None,
                generated_code=None,
                generation_response=response,
                metrics=metrics,
                sandbox_result=None,
                error="Il codice generato è vuoto.",
            )

        # Gli errori di cleanup della sandbox vengono propagati:
        # non devono essere trasformati in normali fallimenti del benchmark.
        sandbox_result = await self.sandbox.execute(
            solution_code=generated_code,
            test_code=benchmark.evaluation.test_code,
            timeout_seconds=benchmark.evaluation.timeout_seconds,
        )

        output = f"{sandbox_result.stdout}\n{sandbox_result.stderr}"
        tests_run, tests_passed = self._parse_test_summary(output)

        score = (
            tests_passed / tests_run
            if tests_run is not None
            and tests_run > 0
            and tests_passed is not None
            else None
        )

        status = sandbox_result.status

        if status == "passed" and (tests_run is None or tests_run == 0) or status == "passed" and tests_passed is None:
            status = "evaluation_error"

        error = sandbox_result.launch_error

        if status == "evaluation_error":
            error = "Riepilogo dei test unittest assente o non valido."

        return BenchmarkResult(
            model=model,
            benchmark_id=benchmark.id,
            benchmark_version=benchmark.version,
            category=benchmark.category,
            status=status,
            score=score,
            tests_run=tests_run,
            tests_passed=tests_passed,
            generated_code=generated_code,
            generation_response=response,
            metrics=metrics,
            sandbox_result=sandbox_result,
            error=error,
        )
