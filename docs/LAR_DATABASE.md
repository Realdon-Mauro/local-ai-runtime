# LAR — Schema SQLite e migrazioni

Riferimenti: LAR_ARCHITECTURE.md e LAR_CONTRACTS.md. Schema proposto; nessuna migration è stata applicata.

## Stato attuale, solo lettura

results/lar.db contiene scans(id, created_at, model_count), model_snapshots(id, scan_id, name, digest, size_bytes, modified_at, metadata_json, inspection_error), benchmark_runs(id, created_at, model, benchmark_id, benchmark_version, category, status, score, tests_run, tests_passed, metrics_json, result_json). model_snapshots ha FK scan_id → scans e unique (scan_id,name); indici su scan/digest e model/benchmark/category/time. Conteggi letti: 3 scans, 42 snapshots, 16 runs. JSON risultato non letto.

BenchmarkStore aggiunge category con ALTER TABLE ad hoc; non c’è schema registry. Mancano hardware, model digest/runtime/spec digest nei run, routing e request metrics.

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

## Migrazione necessaria, non applicata

1. Fermare writer LAR/benchmark e fare SQLite backup verificato.
2. Salvare prima conteggi, IDs, colonne, integrity_check e foreign_key_check.
3. Applicare migration numerata in transazione dove possibile; creare registry e nuove tabelle accanto alle legacy. Primo rilascio non elimina/rinomina tabelle.
4. Importare scans/snapshots e benchmark preservando ID/timestamp/JSON.
5. Digest/hardware/runtime assenti restano NULL con origine legacy_unknown; non dedurli dalla macchina attuale e non considerarli comparabili.
6. Category NULL dei run storici resta NULL. Mantenere lettura legacy sino ad accettazione.
7. Verificare conteggi osservati 3/42/16, IDs, JSON, FK, integrity_check e campione pre/post.
8. Conservare backup/rollback e interrompere su errore parziale.

Test migration: database nuovo, fixture legacy, errore a metà, idempotenza, restore, counts e IDs. Non migrare il database operativo in questa fase.

## Versioni e report

schema_migrations ha versione intera monotona, name/checksum/applied_at. Il programma rifiuta schema più nuovo e verifica checksum. PRAGMA user_version può rispecchiare il registry, non sostituirlo.
