# LAR — Contratti Python e modelli condivisi

Riferimento: LAR_ARCHITECTURE.md. Le interfacce sono proposte, non classi esistenti.

## Scelte di tipo

Protocol ai confini testabili; dataclass frozen/slots per domain objects; tuple/frozenset/read-only mapping per immutabilità; Pydantic per DTO OpenAI al boundary; datetime aware UTC; unità esplicite. AsyncIterator[InferenceEvent] per stream interno, AsyncIterator[bytes] solo per SSE. LARError è record, eccezioni distinte per controllo flusso. Non imporre Pydantic ai tipi interni.

## Interfacce

Firme essenziali. Ogni firma ha dependency, error/invariant e test richiesto.

### LARApplication
Metodi: async start() -> None; async close(*, drain: bool, deadline: datetime | None = None) -> None; async handle(request: InferenceRequest) -> JobHandle; async health() -> SystemHealth.
Responsabilità: composizione, lifecycle, health, use-case application; indipendente da OpenCode.
Dipendenze: contratti gateway/scheduler/provider/repository/config. Errori: LARConfigurationError, ProviderUnavailableError, StorageError, ShutdownError. Invarianti: close blocca submit e chiude risorse una volta. Test: startup down, shutdown con queue/active, idempotenza e leak.

### InferenceProvider
Metodi implementati dal provider v1: `chat(request)` per una risposta completa e `stream_chat(request)` per eventi chat NDJSON; health/discovery/load/unload/generate restano disponibili. I chunk mantengono messaggio assistant, tool calls, `done`, motivo e metriche finali quando presenti.
Responsabilità: unica implementazione Ollama API mapping; il provider restituisce tool call, non li esegue. Nessun retry implicito. La chiusura/cancellazione HTTP non costituisce prova di terminazione fisica; senza verifier affidabile il runtime deve restare bloccato secondo ADR-006.
Dipendenze: HTTP client e DTO provider isolati. Errori: ProviderUnavailableError, ProviderModelNotFoundError, ProviderProtocolError, ProviderTimeoutError, ProviderGenerationError, ProviderStreamError. Invarianti: stream cancellabile, error mapping, no hidden retries after emitted output. Test: MockTransport, invalid shape, HTTP, timeout, disconnect/cancel, tools/capabilities.

### ModelManager
Metodi: async acquire(model_id: str, *, request_id: str) -> ModelLease; async release(lease: ModelLease) -> None; async lease(model_id: str, *, request_id: str) -> AsyncContextManager[ModelLease]; async ensure_loaded(model_id: str) -> ModelState; async unload_if_idle(*, reason: str) -> ModelState; async reconcile() -> Sequence[ModelState]; async get_state(model_id: str | None = None) -> ModelState | Sequence[ModelState].
Responsabilità: ownership/state, load/unload, lease managed model.
Dipendenze: InferenceProvider, clock, policy/repository/event sink. Errori: ModelBusyError, ModelLoadError, ModelUnloadError, LeaseError, ExternalStateConflict, ProviderError. Invarianti: no duplicate load, una lease inferenziale, un managed resident, no unload external, unload error blocca switch. Test: concurrent acquire, switch, load/unload failures, double release, cancel, external model.

### ResourceManager
Metodi: async discover() -> HardwareProfile; async snapshot(*, refresh: bool = False) -> ResourceSnapshot; async estimate(model: ModelInfo, profile: ModelProfile | None, request: InferenceRequest, hardware: HardwareProfile, snapshot: ResourceSnapshot) -> ResourceEstimate; async admit(estimate: ResourceEstimate, policy: ResourcePolicy) -> AdmissionDecision; async simulate(request: InferenceRequest, *, hardware: HardwareProfile, candidates: Sequence[ModelCandidate]) -> SimulationResult.
Responsabilità: discovery/sampling/stima/admission/simulation, non selezione.
Dipendenze: sensor adapters, catalog/profile, ResourcePolicy. Errori: HardwareDiscoveryError se profilo inutile, ResourceEstimationError; sample parziale diventa null. Invarianti: origine measured/declared/estimated/simulated; simulation non chiama provider, lease o mutate state. Test: stale/missing sensors/processes, formula KV/offload, range/classes, no side effects.

### ModelIntelligence
Metodi: async refresh_catalog() -> CatalogSnapshot; async get_candidates(task: TaskProfile) -> Sequence[ModelCandidate]; async get_profile(model: ModelInfo, hardware: HardwareProfile) -> ModelProfile | None; async record_benchmark(result: BenchmarkRecord) -> BenchmarkRunId; async compare(candidates: Sequence[ModelCandidate], *, hardware: HardwareProfile, policy: ComparisonPolicy) -> Sequence[ModelCandidate]; async report(*, hardware_id: str | None = None) -> IntelligenceReport.
Responsabilità: catalog versionato, profili/benchmark/query evidence.
Dipendenze: provider port, benchmark executor/store, repositories. Errori: CatalogError, BenchmarkPersistenceError, ComparisonError. Invarianti: artifact include digest; niente benchmark/request metric mixing; comparability motivata. Test: tag digest, inspect error, missing benchmark, cross-hardware, legacy unknown.

### TaskAnalyzer
Metodo: analyze(request: InferenceRequest) -> TaskProfile.
Responsabilità: estrazione deterministica, no LLM.
Dipendenze: estimator tokenizer opzionale e puro. Errore: TaskAnalysisError se richiesta valida non rappresentabile; token unknown resta unknown. Invarianti: stesso input/version stesso output; non sceglie model. Test: text/image/tools/JSON/output/context/stima unknown.

### ModelRouter
Metodi: async route(request: InferenceRequest, task: TaskProfile, *, catalog: Sequence[ModelCandidate] | None = None) -> RoutingDecision; async revalidate(candidate: ModelCandidate, request: InferenceRequest) -> AdmissionDecision.
Responsabilità: candidate filters, admission/policy, Pareto, alternatives, decision trace.
Dipendenze: intelligence read port, ResourceManager, PolicyEngine; no provider/gateway/LLM. Errori: NoEligibleModelError, ContextTooLargeError, RoutingPolicyError, ResourceRejectedError. Invarianti: snapshot/version saved; reasons per candidate; no cycles. Test: deterministic/missing/non-comparable/incompatible/no fit/tie-break.

### Scheduler
Metodi: async submit(request: InferenceRequest) -> JobHandle; async cancel(request_id: str, *, reason: str = "client_cancelled") -> bool; async queue_status() -> QueueStatus; async close(*, drain: bool, deadline: datetime | None = None) -> None. JobHandle contiene request_id, events: AsyncIterator[InferenceEvent], cancel().
Responsabilità: bounded FIFO, singolo worker inferenziale, cancel/deadline/shutdown.
Dipendenze: execution callback assemblata, router/manager, clock/event sink; no HTTP/SQL specifico. Errori: QueueFullError, RequestExpiredError, RequestCancelledError, SchedulerClosedError. Invarianti: FIFO per sequence; active <=1; queued cancel non chiama provider; cancel active chiude stream/lease; fallback non resetta deadline. Test: barriera FIFO, concurrency count, cap, cancel, deadline, disconnect e close.

### InferenceGateway
Metodi: async complete(payload: OpenAIChatCompletionRequest, *, request_id: str) -> OpenAIChatCompletionResponse; async stream(payload: OpenAIChatCompletionRequest, *, request_id: str) -> AsyncIterator[bytes]; async list_models() -> OpenAIModelsResponse; async health() -> SystemHealth; map_error(error: LARError) -> OpenAIErrorResponse.
Responsabilità: validation, JSON/SSE, lar-auto, OpenAI errors.
Dipendenze: LARApplication/Scheduler e DTO Pydantic; no provider/manager/SQL. Errori core sono HTTP/SSE; validation è pre-submit. Invarianti: request ID stabile; tools restituiti non eseguiti; primo delta vieta fallback. Test: contract, chunk, error pre/post, malformed, tool/usage/cancel.

## Modelli condivisi

Convenzioni: i campi non marcati opzionali sono obbligatori. ID e stringhe identificative non vuote; datetime aware UTC; numeri misurati finiti e non negativi salvo diversa indicazione; percentuali in [0, 100]; intervalli con lower <= upper. Tuple/frozenset sono immutabili. Un campo nullable assente resta None, non zero. Ogni misura/stima include unità e origine. Le decisioni referenziano artifact/profile IDs senza duplicare il catalogo.

| Oggetto | Campi tipizzati, obbligatorietà e validazione | Significato |
|---|---|---|
| ModelInfo | model_id: str; provider_model_id: str; display_name: str; capabilities: frozenset[str]; observed_at: datetime; field_sources: Mapping[str, DataOrigin]; digest: str | None; family: str | None; parameter_count: int | None; artifact_size_bytes: int | None; quantization: str | None; context_limit_tokens: int | None; metadata: Mapping[str, JSONValue] = empty. IDs non vuoti; count/size/context positivi se presenti. | Identità Ollama e metadata scoperti/dichiarati con provenienza per campo. |
| MetricSummary | value: float | None; unit: str; origin: DataOrigin; sample_count: int; hardware_profile_id: str | None; runtime_version: str | None; confidence: float | None. Count >= 0; confidence in [0,1]; unit non vuota. | Un valore di profilo con provenance/comparabilità. |
| ModelProfile | profile_id: str; model_digest: str | None; hardware_profile_id: str | None; runtime_name/version: str | None; profile_version: str; metrics: Mapping[str, MetricSummary]; sample_count: int; coverage: float; confidence: float | None; source_run_ids: tuple[str,...]; created_at: datetime. Count >= 0, coverage/confidence in [0,1]. | Aggregati derivati da run compatibili, hardware/runtime specifici. |
| ModelCandidate | model: ModelInfo; profile: ModelProfile | None; compatibility: CompatibilityReport; resource_estimate: ResourceEstimate | None; admission: AdmissionDecision | None; status: CandidateStatus; reason_codes: tuple[str,...]; evidence_coverage: float; pareto_front: int | None. Coverage in [0,1]; front >= 1 se presente. | Vista immutabile del router, non record catalogo duplicato. |
| ContextRequirements | input_tokens_estimate: int | None; max_output_tokens: int | None; required_context_tokens: int | None; tokenizer_id: str | None; estimation_method: str; error_bound_tokens: int | None; requested_num_ctx: int | None. Token non negativi; metodo non vuoto. | Stima token richiesta separata dal context dichiarato dal modello. |
| TaskProfile | analyzer_version: str; task_tags: frozenset[str]; requires_tools/vision/json: bool; context: ContextRequirements; estimated_output_tokens: int | None; language: str | None; confidence: float | None. Confidence in [0,1]. | Requisiti deterministici estratti senza LLM. |
| RoutingDecision | decision_id/request_id: str; created_at: datetime; router_version/policy_version: str; task_profile_id/hardware_profile_id/resource_snapshot_id: str; selected_model_id/digest: str | None; alternatives: tuple[CandidateRef,...]; candidate_evidence: tuple[CandidateTrace,...]; status: DecisionStatus; reason_codes: tuple[str,...]. ID/version non vuoti. | Audit di decisione con riferimenti agli snapshot usati. |
| GPUInfo | device_key: str; name: str; vendor: str; vram_total_bytes: int | None; compute_capability: str | None; source: str; observed_at: datetime. Byte positivi se presenti. | Inventario statico dispositivo. |
| HardwareProfile | hardware_profile_id/fingerprint: str; cpu_model: str | None; architecture: str; logical_cores: int | None; ram_total_bytes: int | None; gpus: tuple[GPUInfo,...]; os_name/version: str | None; discovery_version: str; discovered_at: datetime; origins: Mapping[str,DataOrigin]. Core/byte positivi se presenti. | Fotografia hardware, non assunzione immutabile. |
| GPUResourceSample | device_key: str; vram_total/free_bytes: int | None; utilization_percent/temperature_c: float | None; source: str; sampled_at: datetime; quality: float | None. VRAM non negativa, free <= total, utilization [0,100]. | Misura dinamica della GPU con sensore/provenienza. |
| ResourceSnapshot | snapshot_id/hardware_profile_id: str; sampled_at: datetime; cpu_utilization_percent: float | None; ram_total/available_bytes: int | None; gpus: tuple[GPUResourceSample,...]; temperatures: tuple[TemperatureSample,...]; external_processes: tuple[ProcessResourceSample,...]; sources: tuple[str,...]; freshness: Freshness; quality: float | None. RAM available <= total. | Misure correnti; sensor assente è None. |
| ResourceComponent | name/unit: str; lower/upper: float; origin: DataOrigin; confidence: float | None; reason: str. Bound >= 0, lower <= upper. | Singola componente stimata/misurata. |
| ResourceEstimate | estimate_id/request_id: str; model_digest: str | None; snapshot_id: str; components: tuple[ResourceComponent,...]; ram_range_bytes: ResourceRange; vram_ranges: Mapping[str,ResourceRange]; offload_fraction: float | None; context_basis/batch_basis: str; confidence: float | None; estimator_version: str; assumptions: tuple[str,...]; origin=estimated. Fraction/confidence [0,1]. | Range espliciti per RAM/VRAM e assunzioni; non garanzia. |
| AdmissionDecision | admission_id/estimate_id/policy_version: str; status: AdmissionLevel; decided_at: datetime; reasons: tuple[str,...]; resource_margin: Mapping[str,float | None]; can_execute: bool. Rejected implica can_execute=False. | Decisione applicativa safe/constrained/rejected. |
| SimulationResult | simulation_id: str; request_id: str | None; synthetic_hardware: HardwareProfile; assumptions: tuple[str,...]; candidate_decisions: tuple[AdmissionDecision,...]; selected_model_id: str | None; simulator_version: str; created_at: datetime; origin=simulated. | Scenario what-if senza inferenza o mutation. |
| ModelState | model_id: str; digest: str | None; status: ModelLifecycleStatus; ownership: ModelOwnership; active_lease_id: str | None; last_reconciled_at: datetime | None; resident_bytes: int | None; last_error: LARError | None. Resident byte non negativi. | Stato Ollama osservato; loaded model distinto da inference active. |
| ModelLease | lease_id/request_id/model_id: str; acquired_at: datetime; expires_at: datetime | None; owner: ModelOwnership; state: LeaseStatus; generation_no: int. Generation >= 1; owner deve essere lar per lease inferenziale. | Possesso temporaneo per una singola inference. |
| ChatMessage | role: system/developer/user/assistant/tool; content: str | tuple[ContentPart,...]; name/tool_call_id: str | None. Role/content validati OpenAI; tool response riferita a call esistente. | Messaggio volatile di una richiesta chat. |
| ToolDefinition | tool_id: str; name: str; description: str | None; parameters_schema: Mapping[str,JSONValue]; strict: bool | None. Nome/schema validati al boundary. | Descrizione tool inoltrata al modello, non eseguita da LAR. |
| InferenceRequest | request_id: str; created_at/deadline_at: datetime; model_alias: str; messages: tuple[ChatMessage,...]; tools: tuple[ToolDefinition,...]; tool_choice: str | Mapping | None; response_format: Mapping | None; stream: bool; max_output_tokens: int | None; temperature: float | None; stop: tuple[str,...]; safe_client_metadata: Mapping[str,str]; content_policy: ContentPolicy. Temperature secondo schema supportato dal modello; deadline > created. | DTO di dominio dopo validazione; raw content non persistito di default. |
| InferenceEvent | request_id: str; sequence_no: int; occurred_at: datetime; kind: EventKind; data: ContentDelta | ToolCallDelta | UsageEvent | ErrorEvent | CompletionEvent; output_committed: bool. Sequence >= 0; event data coerente col kind. | Eventi ordinati dallo scheduler/provider al gateway. |
| InferenceMetrics | request_id: str; queue_wait_ms/model_load_ms/first_token_ms/total_ms: float | None; prompt/completion_tokens: int | None; generation_tokens_per_second: float | None; cpu_peak_percent: float | None; ram/vram_peak_bytes: int | None; temperature_peak_c: float | None; sources: Mapping[str,str]. Durate/count non negativi. | Metriche traffico ordinario, separate dai benchmark. |
| QueueStatus | queued_count/capacity/next_sequence_no: int; active_request_id: str | None; active_since: datetime | None; accepting: bool; worker_state: WorkerState; observed_at: datetime. 0 <= queued <= capacity; sequence non decrescente. | Stato della FIFO bounded; active request al massimo uno. |
| ComponentHealth | status: healthy/degraded/unavailable; checked_at: datetime; message: str | None; details: Mapping[str,JSONValue]. | Stato di singolo sottosistema. |
| SystemHealth | status: healthy/degraded/unavailable; provider/storage/resources/scheduler/model_manager: ComponentHealth; checked_at: datetime; warnings: tuple[str,...]. | Health provider non equivale a readiness inference. |
| LARError | code/message/stage: str; request_id: str | None; retryable: bool; http_status: int; details: Mapping[str,JSONValue]; cause_type: str | None; occurred_at: datetime. Nessun prompt/secret/stack pubblico. | Errore correlabile e mappabile a OpenAI. |

Tipi ausiliari espliciti: DataOrigin = measured/model_declared/estimated/simulated; CandidateStatus = eligible/unscored/constrained/rejected/incompatible/unavailable; AdmissionLevel = safe/constrained/rejected; ModelLifecycleStatus = unloaded/loading/loaded/unloading/external/unknown; ModelOwnership = lar/external/unknown; LeaseStatus = active/released/expired; EventKind = queued/routing/loading/content_delta/tool_call_delta/usage/completed/error. ResourceRange contiene lower_bytes, upper_bytes, unit e origin. CompatibilityReport elenca capability mancanti/context fit. CandidateRef e CandidateTrace usano artifact/profile/estimate IDs, rank/status/reasons. TemperatureSample e ProcessResourceSample includono value/source/unit/timestamp e nullable per telemetria non supportata.

## Eccezioni

LARError è record pubblico; gerarchia runtime: LARException; ValidationError; QueueFullError/RequestExpiredError/RequestCancelledError; NoEligibleModelError/ContextTooLargeError/ResourceRejectedError; ProviderUnavailableError/ProviderProtocolError/ProviderModelNotFoundError/ProviderLoadError/ProviderGenerationError/ProviderStreamError; ModelBusyError/LeaseError/ModelLoadError/ModelUnloadError/ExternalStateConflict; StorageError/HardwareDiscoveryError/ResourceEstimationError/ShutdownError.

Retryability dipende anche da fase, output committed, deadline e modelli tentati, non solo dalla classe.
