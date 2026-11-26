# LAR — Schema SQLite e migrazioni

Riferimenti: LAR_ARCHITECTURE.md e LAR_CONTRACTS.md. È implementata la migration runtime v1 in `src/lar/storage/database.py`; nessuna migration è applicata automaticamente a `results/lar.db`.

## Stato attuale, solo lettura

results/lar.db contiene scans(id, created_at, model_count), model_snapshots(id, scan_id, name, digest, size_bytes, modified_at, metadata_json, inspection_error), benchmark_runs(id, created_at, model, benchmark_id, benchmark_version, category, status, score, tests_run, tests_passed, metrics_json, result_json). model_snapshots ha FK scan_id → scans e unique (scan_id,name); indici su scan/digest e model/benchmark/category/time. Conteggi letti: 3 scans, 42 snapshots, 16 runs. JSON risultato non letto.

Il database benchmark storico conserva il proprio schema e `BenchmarkStore` aggiunge category con ALTER TABLE ad hoc. Il nuovo storage runtime ha migration registry con checksum e schema additivo per snapshot config non-segreti, hardware/risorse, artefatti/profili, benchmark, richieste/routing/metriche, lifecycle e outbox osservativa. I due storage non sono uniti automaticamente.

## Tabelle proposte

### Migration/config
- schema_migrations: PK version, name unique, checksum, applied_at. Rileva doppia applicazione e modifica di migration già usata.
- config_snapshots: PK config_id, version, created_at, config_json, source, active boolean; indice active/version, un solo profilo attivo. Mai secret.

### Hardware
- hardware_profiles: PK profile_id, unique fingerprint, discovered_at, discovery_version, CPU/memory/GPU/OS JSON, source-quality JSON.
- resource_snapshots: PK snapshot_id, FK hardware_profile, sampled_at, CPU JSON, RAM total/available bytes, external processes, sources, freshness, quality. Indice hardware/time DESC.
- gpu_resource_samples: PK sample_id, FK snapshot CASCADE, device_key, VRAM total/free, utilization, temp, source, quality; unique snapshot/device e indice device/time.

### Model
- model_artifacts: PK artifact_id, provider CHECK ollama, provider_model_id, digest?, first/last seen, model_info_json, field_sources_json, inspection_status. Identità provider+digest; tag può cambiare digest.
- model_profiles: PK profile_id, FK artifact, FK nullable hardware, runtime/version, profile_version, created_at, profile JSON, sample_count >=0, confidence. Unique artifact/hardware/runtime/version e indici artifact/hardware.

### Benchmark, separato da request metrics
- benchmark_specs: PK benchmark_id + benchmark_version, category, spec_digest, spec_json, created_at; versione immutabile.
- benchmark_runs: PK run_id, FK spec composta, FK artifact, FK nullable hardware per legacy, started/ended, status, runner/runtime versions, context, generation_options_json, environment_json minimizzato, error_code. Indici artifact/time, benchmark/version/hardware, status/time.
- benchmark_measurements: PK measurement_id, FK run CASCADE, metric_name, value nullable, unit, direction minimize/maximize/target/none, origin measured/model_declared/estimated, confidence, sample_count. Score, test outcome, tempi, throughput, RAM/VRAM sono righe. Missing è NULL.

Comparability richiede spec digest/version, model digest/quantization, runtime e hardware previsti dalla ComparisonPolicy. Legacy senza hardware resta leggibile, ma non è confronto hardware-specific.

### Requests/routing/lifecycle
- inference_requests: PK request_id, created/completed, client_model_alias, selected_artifact nullable FK, status, finish_reason, error, stream boolean, tool_count, content_persisted boolean. Non include prompt/completion/tool args/auth headers.
- routing_decisions: PK decision_id, FK unique request_id; FK hardware/snapshot/config; timestamp, router/policy/analyzer versions, status, reason_codes_json, task profile non sensibile. Indice time/status.
- routing_candidates: PK decision_id + ordinal; FK decision, artifact/profile/estimate nullable; eligible, status, reasons, Pareto front, evidence coverage. Indice artifact/decision. Non duplica ModelInfo.
- request_metrics: PK/FK request_id; queue/load/first-token/total ms, prompt/output tokens, generation tps, CPU/RAM/VRAM/temp peak, measurement sources. Nullable, non benchmark.
- model_lifecycle_events: PK event_id, timestamp, artifact/request nullable, event type, ownership/state before/after, duration/error/details. Indici model/time e request/time. Lease resta in memoria e non sopravvive come attiva a crash.

## Relazioni e vincoli

FK abilitate per connessione; CHECK su state/origin/percentuali/count; null ammesso per sensori assenti; RESTRICT su artifact/hardware/spec referenziati, CASCADE solo su righe figlie possedute. Config senza segreti. JSON versionato; misure queryable normalizzate. Benchmark e normali richieste hanno storage/retention separati. Report cita run inclusi/esclusi e motivi.

## Migrazioni runtime v1–v2

All’avvio il runtime crea/aggiorna solo il percorso database runtime configurato, che per default è `~/.local/state/lar/lar.sqlite3`. Se il file esiste e richiede una migration, prima crea un backup SQLite e ne verifica l’integrità; poi applica DDL additivo in transazione, registra checksum e versione, imposta `user_version`, esegue `integrity_check` e `foreign_key_check`. Le riesecuzioni con checksum valido sono idempotenti. Checksum diverso o integrity error interrompono l’avvio.

La v1 crea le tabelle runtime e il registro checksum. La v2 aggiunge `retention_audit`. Il job `Database.purge_expired` eseguito all’avvio e con `lar db prune` applica i periodi ADR-004 configurati: snapshot risorse 7 giorni, metriche/richieste 365 giorni, eventi/lifecycle 14 giorni. Le osservazioni outbox `pending` non vengono eliminate; soltanto righe `delivered`/`acked` oltre 14 giorni sono rimosse. Ogni passaggio registra quantità cancellate e hash policy in `retention_audit`. I benchmark, i cataloghi e la provenienza non sono cancellati. Gli snapshot risorsa scaduti sganciano prima il riferimento nullable dalle decisioni.

Le osservazioni OpenCode ricevute dal gateway sono inserite append-only in `observation_events`; solo `source + native_id` deduplica. Un nuovo evento `user_message_received` riceve un `workflow_id` generato da LAR. Plugin, listener e outbox client non sono ancora implementati. Gli eventi senza prova restano non correlati; non si ricostruiscono sessioni.

Il percorso `results/lar.db` viene rifiutato come database runtime. `lar db import-legacy-benchmarks --source PATH` è una procedura esplicita di sola lettura che copia i benchmark in un database runtime già inizializzato, dopo averne creato un backup verificato. Preserva ID legacy nella provenance, modelli/digest quando presenti, status e misure; i campi hardware/runtime/spec non disponibili restano NULL o `legacy_unknown` e la comparabilità è sempre `unscored`. Il JSON contenente codice o risposte non viene importato. L’import ripetuto dalla stessa source path è idempotente. Le tabelle scans/snapshots originali restano intatte; i metadati snapshot alimentano gli artifact, senza ricostruire scans come eventi runtime.

## Import legacy controllato

1. Inizializzare e verificare il database runtime, poi invocare esplicitamente il comando con il percorso sorgente.
2. Aprire la sorgente in sola lettura, validare tabelle/colonne e fare backup verificato del target.
3. Importare artifact snapshot e benchmark/metriche in transazione; non leggere né copiare result JSON con contenuto generato.
4. Digest/artifact noti sono conservati; hardware/runtime/spec/options assenti restano `legacy_unknown` e non comparabili.
5. Category NULL e timestamp/ID legacy restano preservati nella misura supportata dallo schema runtime.
6. Verificare conteggi source/import, FK e integrity_check; l’operazione ripetuta è idempotente.
7. La sorgente non è modificata; rollback riguarda solo la transazione target e il backup verificato resta disponibile.

Test migration: database nuovo, fixture legacy, errore a metà, idempotenza, restore, counts e IDs. Non migrare il database operativo in questa fase.

## Versioni e report

schema_migrations ha versione intera monotona, name/checksum/applied_at. Il programma rifiuta schema più nuovo e verifica checksum. PRAGMA user_version può rispecchiare il registry, non sostituirlo.
