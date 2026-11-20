# LAR — Gap analysis implementativa

Analisi statica di ~/local-ai-runtime al 20 novembre 2026. Non esiste .git, quindi git status --short non è disponibile. Nessun test eseguito; ~/benchmark-ai non è stato letto. results/lar.db ispezionato in sola lettura.

## Sintesi

Il repository implementa CLI inventory/benchmark per Ollama: provider, discovery, catalogo snapshot, runner sandboxed, metriche e ranking. Non implementa ancora chat orchestration. Non esistono documenti in docs/ né config attiva in config/. Le directory src/lar/api, classifiers, monitoring, storage risultano vuote. FastAPI/Uvicorn/Pydantic sono dichiarate in pyproject.toml, ma non usate dal codice attivo.

## Stato per componente

| Componente | Stato | Evidenza | Discrepanza |
|---|---|---|---|
| Ollama provider | Parziale, riusabile | providers/base.py GenerationProvider; providers/ollama.py health, tags, ps, load/unload, generate non-stream, show, error mapping. | Solo /api/generate; no chat/SSE/tool/cancel DTO. |
| Discovery | Parziale | evaluation/discovery.py legge digest/size/metadata/capabilities/inspection error. | Serial, dataclass mutabile, no profili runtime/hardware. |
| Catalogo | Parziale | evaluation/catalog.py scans/snapshots con FK/indici e storico. | Nessun artifact normalized identity/schema registry. |
| ModelManager | Parziale | core/model_manager.py lock, lease context, active count per modello, ownership external. | Non limita globalmente managed model/inferenza, né FIFO; più modelli possono restare caricati; ModelState mutabile; niente request ID/idle/reconcile state esplicito. |
| Scheduler | Assente | Nessuna coda/worker. | FIFO, bounded queue, cancel, deadline, backpressure e shutdown assenti. |
| Orchestrazione/chat | Assente | Orchestrator solo per batch benchmark. | Mancano LARApplication, Analyzer, Router, Policy, inference fallback. |
| API OpenAI | Assente | Nessun route FastAPI/DTO/SSE/model endpoint. | OpenCode non può usare LAR come proxy. |
| Resource Manager | Assente | psutil dichiarata ma non usata in discovery runtime. | Mancano CPU/RAM/GPU/VRAM/temp, estimator, admission, simulation. |
| Model Intelligence | Parziale | evaluation ha benchmark specs, discovery, metrics e tre ranking. | Un solo YAML; ranking weights opzionali; no comparability hardware/digest/runtime. |
| Benchmark | Implementato per use case ristretto | runner/orchestrator/CLI, systemd/bwrap/limits/ro-bind/seccomp denylist. | Non chat routing; sandbox tests non eseguono systemd reale e2e. Seccomp denylist con default allow, non allowlist completa. |
| Observability | Parziale | GenerationMetrics e output CLI. | Niente request ID/JSON log/live resource metrics/routing reasons. |
| SQLite | Parziale | ModelCatalog/BenchmarkStore e FK pragma/index; test legacy category. | ALTER ad hoc, no version registry, no request/hardware/decision tables. |
| Privacy | Da definire | BenchmarkResult serializza generated_code e generation_response nel result_json. | Retention/accesso da definire; traffico normale senza content per default. |
| Config | Assente/indeterminata | config/ vuota; CLI default relativi al CWD. | Nessun config versionato, resource reserve, queue, bind, timeout, retention. |
| OpenCode | Assente | Nessun endpoint o e2e test. | Serve solo come client test; non dependency del core. |

## Benchmark integrato

benchmarks/coding/file_stats.yaml è versione 2, categoria coding, capability completion, valutazione python_unittest; unico YAML rilevato. BenchmarkRunner genera codice dal provider, lo valuta in sandbox e raccoglie metriche; orchestrator usa lease; CLI offre scan, history, run/all/ranking.

Run conserva nome modello, id/version benchmark, category, status, score/test, metriche e JSON completo. Non conserva hardware, artifact digest, runtime version o spec digest. Ranking prende ultima versione e ultimo run valido per modello/benchmark, medie categoria, overall con pesi fornibili e throughput separato. Non basta a un router cross-hardware. Non sono stati letti o inventati risultati numerici.

## Test osservati, non eseguiti

13 moduli coprono provider non-stream/error mapping; discovery/catalog history; lease e batch sequencing; spec loader/runner/metrics; legacy category migration; ranking/CLI; command builder e cleanup sandbox simulato; seccomp BPF. Mancano API/SSE/tool calls, FIFO concorrente, serialità scheduler, router/fallback runtime, resource/admission/simulation, OOM effettivo in cgroup, Ollama stream e OpenCode e2e.

## Debito tecnico/affidabilità

1. DB senza migration registry; BenchmarkStore applica ALTER ad hoc.
2. Dict provider e dataclass mutabili hanno validazione metadata limitata.
3. Lock protegge duplicate load nella stessa manager, non concurrency o resident models globali.
4. Ranking non ha provenance per hardware/digest/runtime/spec; pesi non calibrati.
5. Manca lifecycle server/health/queue recovery e client esterni possono condividere Ollama.
6. CLI paths relativi alla working directory.
7. Metriche limitate a risposta generate; niente first token, risorse o telemetry.
8. Sandbox multi-layer con cleanup attento; seccomp denylist, efficacy legata a kernel, namespace, user systemd. Non descriverla come isolamento assoluto.
9. JSON benchmark conserva risposta/codice; retention e accesso necessari.
10. DB operativo: 3 scans, 42 snapshots, 16 runs; nessuna modifica eseguita.

## Componenti da riusare

GenerationUsage, GenerationResponse, GenerationProvider, OllamaProvider; ModelManager.lease come punto di partenza dopo rinforzo invarianti; ModelInventoryEntry, ModelCatalog, BenchmarkSpec, BenchmarkRunner, BenchmarkStore, GenerationMetrics e test. SandboxExecutor resta nel solo benchmark. Non riscrivere componenti funzionanti per stile.

## Rischi top

Controllo non esclusivo su Ollama esterno; confine tool execution; ranking attuale non comparabile; migrazione senza backup; estimate presentato come measure; API non-loopback senza auth o logging di prompt/header.
