
from collections.abc import Sequence

from lar.core.model_manager import ModelManager
from lar.evaluation.results import BenchmarkStore
from lar.evaluation.runner import BenchmarkResult, BenchmarkRunner
from lar.evaluation.specs import BenchmarkSpec


class BenchmarkOrchestrator:
    """Coordina modelli, benchmark e persistenza dei risultati."""

    def __init__(
        self,
        model_manager: ModelManager,
        runner: BenchmarkRunner,
        store: BenchmarkStore | None = None,
    ) -> None:
        self.model_manager = model_manager
        self.runner = runner
        self.store = store

    async def run(
        self,
        model: str,
        benchmark: BenchmarkSpec,
    ) -> BenchmarkResult:
        """Esegue un benchmark e ne salva il risultato."""

        result, _ = await self.run_with_id(model, benchmark)
        return result

    async def run_with_id(
        self,
        model: str,
        benchmark: BenchmarkSpec,
    ) -> tuple[BenchmarkResult, int | None]:
        """Esegue un benchmark e restituisce anche l'ID persistito."""

        async with self.model_manager.lease(model):
            result = await self.runner.run(model, benchmark)

        run_id = None
        if self.store is not None:
            run_id = self.store.save_result(result)

        return result, run_id


    async def run_batch_for_model(
        self,
        model: str,
        benchmarks: Sequence[BenchmarkSpec],
    ) -> list[BenchmarkResult]:
        """Esegue benchmark sequenziali mantenendo un solo lease per modello."""

        results = []

        try:
            async with self.model_manager.lease(model):
                for benchmark in benchmarks:
                    result = await self.runner.run(model, benchmark)
                    results.append(result)

                    if self.store is not None:
                        self.store.save_result(result)
        finally:
            await self.model_manager.unload(model)

        return results

    async def run_many(
        self,
        model: str,
        benchmarks: Sequence[BenchmarkSpec],
    ) -> list[BenchmarkResult]:
        """Esegue più benchmark sullo stesso modello, in sequenza."""

        results = []

        for benchmark in benchmarks:
            result = await self.run(model, benchmark)
            results.append(result)

        return results
