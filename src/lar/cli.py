import argparse
import asyncio
import sys
from pathlib import Path

from lar.core.model_manager import ModelManager
from lar.evaluation.catalog import ModelCatalog
from lar.evaluation.discovery import ModelInventoryEntry, discover_models
from lar.evaluation.orchestrator import BenchmarkOrchestrator
from lar.evaluation.ranking import build_rankings
from lar.evaluation.results import BenchmarkStore
from lar.evaluation.runner import BenchmarkRunner
from lar.evaluation.specs import (
    BenchmarkSpecError,
    load_benchmarks,
)
from lar.providers.ollama import OllamaError, OllamaProvider

DEFAULT_DATABASE = Path("results/lar.db")
DEFAULT_BENCHMARKS_DIR = Path("benchmarks")


def format_size(size_bytes: int) -> str:
    return f"{size_bytes / (1024 ** 3):.2f} GB"


def print_model(model: ModelInventoryEntry, index: int, total: int) -> None:
    print(f"\n[{index}/{total}] {model.name}")
    print(f"  Dimensione:     {format_size(model.size_bytes)}")
    print(f"  Famiglia:       {model.family or 'N/D'}")
    print(f"  Parametri:      {model.parameter_size or 'N/D'}")
    print(f"  Quantizzazione: {model.quantization_level or 'N/D'}")
    print(f"  Formato:        {model.format or 'N/D'}")
    print(f"  Capacità:       {', '.join(model.capabilities) or 'N/D'}")
    print(f"  Digest:         {(model.digest or 'N/D')[:16]}")

    if model.inspection_error:
        print(f"  Errore:         {model.inspection_error}")


async def run_scan(database_path: Path, show: bool) -> int:
    catalog = ModelCatalog(database_path)

    try:
        async with OllamaProvider() as provider:
            if not await provider.health_check():
                print("Errore: Ollama non è disponibile.", file=sys.stderr)
                return 1

            print("Scansione dei modelli Ollama in corso...")
            models = await discover_models(provider)

    except OllamaError as exc:
        print(f"Errore durante la comunicazione con Ollama: {exc}", file=sys.stderr)
        return 1

    scan_id = catalog.save_scan(models)

    print("\nScansione completata.")
    print(f"ID scansione: {scan_id}")
    print(f"Modelli trovati: {len(models)}")
    print(f"Database: {database_path}")

    errors = sum(model.inspection_error is not None for model in models)
    if errors:
        print(f"Modelli con errori di ispezione: {errors}")

    if show:
        for index, model in enumerate(models, 1):
            print_model(model, index, len(models))

    return 0


def run_history(database_path: Path) -> int:
    if not database_path.exists():
        print(f"Nessuno storico trovato. Database assente: {database_path}")
        return 0

    catalog = ModelCatalog(database_path)
    scans = catalog.list_scans()

    if not scans:
        print("Nessuna scansione registrata.")
        return 0

    print(f"Storico scansioni: {database_path}\n")

    for scan in scans:
        print(
            f"ID: {scan['id']} | "
            f"Data: {scan['created_at']} | "
            f"Modelli: {scan['model_count']}"
        )

    return 0


def find_benchmark(benchmarks_dir: Path, benchmark_id: str):
    """Trova una specifica tramite il suo ID."""

    benchmarks = load_benchmarks(benchmarks_dir)

    for benchmark in benchmarks:
        if benchmark.id == benchmark_id:
            return benchmark

    raise BenchmarkSpecError(
        f"Benchmark non trovato: {benchmark_id} "
        f"(directory: {benchmarks_dir})"
    )


async def run_benchmark(
    benchmark_id: str,
    model: str,
    database_path: Path,
    benchmarks_dir: Path,
) -> int:
    try:
        benchmark = find_benchmark(benchmarks_dir, benchmark_id)

        async with OllamaProvider() as provider:
            await provider.health_check()

            installed_models = await provider.list_models()
            installed_names = {
                name
                for item in installed_models
                for name in (item.get("name"), item.get("model"))
                if isinstance(name, str)
            }

            if model not in installed_names:
                available = ", ".join(sorted(installed_names)) or "nessuno"
                print(
                    f"Errore: modello '{model}' non trovato in Ollama. "
                    f"Modelli disponibili: {available}",
                    file=sys.stderr,
                )
                return 1

            manager = ModelManager(provider)
            runner = BenchmarkRunner(provider)
            store = BenchmarkStore(database_path)

            orchestrator = BenchmarkOrchestrator(
                model_manager=manager,
                runner=runner,
                store=store,
            )

            print(f"Benchmark: {benchmark.id} v{benchmark.version}")
            print(f"Modello: {model}")
            print("Esecuzione in corso...")

            result, run_id = await orchestrator.run_with_id(model, benchmark)

    except BenchmarkSpecError as exc:
        print(f"Errore nella specifica: {exc}", file=sys.stderr)
        return 1
    except OllamaError as exc:
        print(f"Errore Ollama: {exc}", file=sys.stderr)
        return 1

    print("\nBenchmark completato.")
    print(f"ID esecuzione: {run_id}")
    print(f"Stato: {result.status}")
    print(f"Punteggio: {result.score if result.score is not None else 'N/D'}")
    print(f"Test superati: {result.tests_passed}/{result.tests_run}")
    print(f"Modello: {result.model}")
    print(f"Database: {database_path}")

    if result.metrics.generation_tokens_per_second is not None:
        print(
            "Velocità generazione: "
            f"{result.metrics.generation_tokens_per_second:.2f} token/s"
        )

    if result.error:
        print(f"Dettaglio: {result.error}")

    return 0



async def run_benchmark_all(
    database_path: Path,
    benchmarks_dir: Path,
    category: str | None = None,
    selected_model: str | None = None,
) -> int:
    """Esegue tutti i benchmark compatibili sui modelli installati."""

    try:
        benchmarks = load_benchmarks(benchmarks_dir, category=category)

        if not benchmarks:
            print("Nessun benchmark trovato per i criteri richiesti.", file=sys.stderr)
            return 1

        async with OllamaProvider() as provider:
            if not await provider.health_check():
                print("Errore: Ollama non è disponibile.", file=sys.stderr)
                return 1

            models = await discover_models(provider)

            if selected_model is not None:
                models = [model for model in models if model.name == selected_model]

                if not models:
                    print(
                        f"Errore: modello '{selected_model}' non trovato in Ollama.",
                        file=sys.stderr,
                    )
                    return 1

            manager = ModelManager(provider)
            runner = BenchmarkRunner(provider)
            store = BenchmarkStore(database_path)
            orchestrator = BenchmarkOrchestrator(manager, runner, store)

            executed = 0
            skipped = 0

            print(f"Benchmark disponibili: {len(benchmarks)}")
            print(f"Modelli individuati: {len(models)}")

            for index, model in enumerate(models, 1):
                print(f"\n[{index}/{len(models)}] Modello: {model.name}")

                if model.inspection_error:
                    print(f"  SALTATO: metadati non disponibili: {model.inspection_error}")
                    skipped += len(benchmarks)
                    continue

                capabilities = set(model.capabilities)
                compatible = [
                    benchmark
                    for benchmark in benchmarks
                    if set(benchmark.required_capabilities).issubset(capabilities)
                ]

                incompatible = len(benchmarks) - len(compatible)
                skipped += incompatible

                if incompatible:
                    print(f"  Benchmark incompatibili: {incompatible}")

                if not compatible:
                    print("  Nessun benchmark compatibile.")
                    continue

                print(f"  Benchmark da eseguire: {len(compatible)}")

                try:
                    results = await orchestrator.run_batch_for_model(
                        model.name,
                        compatible,
                    )
                except OllamaError as exc:
                    print(f"  ERRORE Ollama: {exc}", file=sys.stderr)
                    print("  Modello saltato. Passo al successivo.")
                    skipped += len(compatible)
                    continue

                executed += len(results)

                for result in results:
                    score = (
                        f"{result.score:.2f}"
                        if result.score is not None
                        else "N/D"
                    )
                    print(
                        f"  {result.benchmark_id}: "
                        f"{result.status} | punteggio: {score}"
                    )

            print("\nBatch completato.")
            print(f"Benchmark eseguiti: {executed}")
            print(f"Benchmark saltati: {skipped}")
            print(f"Database: {database_path}")

            return 0 if executed else 1

    except BenchmarkSpecError as exc:
        print(f"Errore nelle specifiche: {exc}", file=sys.stderr)
        return 1
    except OllamaError as exc:
        print(f"Errore Ollama: {exc}", file=sys.stderr)
        return 1



def run_benchmark_ranking(
    database_path: Path,
    category: str | None = None,
) -> int:
    """Mostra le classifiche calcolate sullo storico completo."""

    if not database_path.exists():
        print(f"Database non trovato: {database_path}", file=sys.stderr)
        return 1

    store = BenchmarkStore(database_path)
    runs = store.list_runs(limit=None)

    if category is not None:
        runs = [run for run in runs if run.get("category") == category]

    if not runs:
        print("Nessun risultato disponibile per i criteri richiesti.")
        return 0

    rankings = build_rankings(runs)

    print(f"Classifiche benchmark: {database_path}")

    if category:
        print(f"Categoria selezionata: {category}")

    print("\n=== RANKING PER CATEGORIA ===")

    if not rankings["categories"]:
        print("Nessun risultato valido.")
    else:
        for category_name, entries in rankings["categories"].items():
            print(f"\n[{category_name}]")

            for entry in entries:
                print(
                    f"  #{entry['rank']} {entry['model']} | "
                    f"{entry['score']:.2f}/100 | "
                    f"Benchmark: {entry['benchmarks_count']}"
                )

    print("\n=== RANKING GENERALE ===")

    if not rankings["overall"]:
        print("Nessun risultato valido.")
    else:
        for entry in rankings["overall"]:
            print(
                f"  #{entry['rank']} {entry['model']} | "
                f"{entry['score']:.2f}/100 | "
                f"Categorie: {entry['categories_count']}"
            )

    print("\n=== RANKING EFFICIENZA ===")

    if not rankings["efficiency"]:
        print("Metriche di throughput non disponibili.")
    else:
        for entry in rankings["efficiency"]:
            print(
                f"  #{entry['rank']} {entry['model']} | "
                f"{entry['generation_tokens_per_second']:.2f} token/s | "
                f"Durata: {entry['total_duration_seconds']!s}s | "
                f"Caricamento: {entry['load_duration_seconds']!s}s"
            )

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lar-bench",
        description="Benchmark e inventario dei modelli Ollama.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser(
        "scan",
        help="Scansiona i modelli installati in Ollama.",
    )
    scan_parser.add_argument(
        "--show",
        action="store_true",
        help="Mostra i dettagli dei modelli individuati.",
    )
    scan_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help=f"Percorso del database SQLite (default: {DEFAULT_DATABASE}).",
    )

    history_parser = subparsers.add_parser(
        "history",
        help="Mostra lo storico delle scansioni.",
    )
    history_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help=f"Percorso del database SQLite (default: {DEFAULT_DATABASE}).",
    )

    benchmark_parser = subparsers.add_parser(
        "benchmark",
        help="Gestisce i benchmark.",
    )
    benchmark_subparsers = benchmark_parser.add_subparsers(
        dest="benchmark_command",
        required=True,
    )

    all_parser = benchmark_subparsers.add_parser(
        "all",
        help="Esegue tutti i benchmark compatibili sui modelli locali.",
    )
    all_parser.add_argument(
        "--model",
        help="Limita l'esecuzione a un singolo modello Ollama.",
    )
    all_parser.add_argument(
        "--category",
        help="Limita l'esecuzione a una categoria di benchmark.",
    )
    all_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help=f"Percorso del database SQLite (default: {DEFAULT_DATABASE}).",
    )
    all_parser.add_argument(
        "--benchmarks-dir",
        type=Path,
        default=DEFAULT_BENCHMARKS_DIR,
        help=f"Directory delle specifiche (default: {DEFAULT_BENCHMARKS_DIR}).",
    )

    ranking_parser = benchmark_subparsers.add_parser(
        "ranking",
        help="Mostra le classifiche dei benchmark registrati.",
    )
    ranking_parser.add_argument(
        "--category",
        help="Limita le classifiche a una categoria.",
    )
    ranking_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help=f"Percorso del database SQLite (default: {DEFAULT_DATABASE}).",
    )

    run_parser = benchmark_subparsers.add_parser(
        "run",
        help="Esegue un benchmark su un modello.",
    )
    run_parser.add_argument(
        "benchmark_id",
        help="ID della specifica, ad esempio coding.file_stats.",
    )
    run_parser.add_argument(
        "--model",
        required=True,
        help="Nome del modello Ollama da utilizzare.",
    )
    run_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help=f"Percorso del database SQLite (default: {DEFAULT_DATABASE}).",
    )
    run_parser.add_argument(
        "--benchmarks-dir",
        type=Path,
        default=DEFAULT_BENCHMARKS_DIR,
        help=f"Directory delle specifiche (default: {DEFAULT_BENCHMARKS_DIR}).",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "scan":
        return asyncio.run(run_scan(args.database, args.show))

    if args.command == "history":
        return run_history(args.database)

    if args.command == "benchmark" and args.benchmark_command == "ranking":
        return run_benchmark_ranking(args.database, args.category)

    if args.command == "benchmark" and args.benchmark_command == "all":
        return asyncio.run(
            run_benchmark_all(
                args.database,
                args.benchmarks_dir,
                args.category,
                args.model,
            )
        )

    if args.command == "benchmark" and args.benchmark_command == "run":
        return asyncio.run(
            run_benchmark(
                args.benchmark_id,
                args.model,
                args.database,
                args.benchmarks_dir,
            )
        )

    parser.error("Comando non riconosciuto.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
