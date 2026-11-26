# LAR — Architettura

**Stato:** specifica progettuale v1, basata sull’ispezione statica del repository il 20 novembre 2026 e integrata con le evidenze benchmark del 16 novembre e dell’audit OpenCode v2.2.7. Descrive l’architettura target, non funzionalità già implementate. L’implementazione verificata resta circoscritta a inventory/benchmark CLI e al singolo ciclo documentato in `LAR_IMPLEMENTATION_GAP_ANALYSIS.md`; i componenti progettati per chat orchestration e gateway non risultano implementati. I test non sono stati eseguiti in questa revisione.

## Visione e confini

Local AI Runtime è un servizio locale che seleziona e governa l’inferenza per applicazioni client. OpenCode è il primo client; LAR resta indipendente da OpenCode e inizialmente espone HTTP compatibile OpenAI con Ollama come unico backend.

- OpenCode gestisce agenti, filesystem, terminale, strumenti, skill e workflow.
- LAR gestisce scelta modello, risorse, coda, lifecycle, fallback, inferenza e telemetria.
- Ollama esegue i modelli. Endpoint, payload e dettagli provider restano confinati nell’adapter Ollama.
- I benchmark rilevanti sono quelli integrati in questo repository. ~/benchmark-ai è fuori ambito.

Prima versione: applicazione modulare singola, non microservizi. FastAPI è il boundary HTTP; SQLite lo storage iniziale.

## Invarianti architetturali

1. Routing deterministico, senza secondo LLM.
2. Autonomia con tracciabilità: versioni policy/algoritmo, candidati, evidenze, esito e motivazione.
3. Una inferenza LAR attiva per volta; scheduler FIFO.
4. Al massimo un modello loaded da LAR. Modelli external non sono proprietà LAR.
5. Modelli residenti e inferenze attive sono conteggi separati.
6. Ogni candidato viene provato al massimo una volta per richiesta. Nessun retry dopo output consegnato.
7. Input malformato e cancel utente non attivano fallback.
8. Misurato, dichiarato, stimato e simulato sono valori con provenienza esplicita.
9. Benchmark confrontati solo a parità dei requisiti di compatibilità definiti dalla policy.
10. Prompt/completion non persistiti per default.
11. Le stime sono intervalli/rischi, non garanzie.

### Limite sull’istanza Ollama

Serialità e lease sono garantite per richieste inviate da LAR. Client esterni sulla stessa istanza possono generare e caricare modelli fuori dal controllo LAR. LAR rileva lo stato osservabile e non scarica modelli esterni. Se si richiede esclusività sull’intero host, va decisa un’ulteriore strategia di isolamento/access control; la API Ollama condivisa non consente a LAR di imporre da sola questa garanzia.

## Sottosistemi

### API Gateway
FastAPI espone POST /v1/chat/completions JSON e SSE, GET /v1/models con modello virtuale lar-auto e health/readiness locale. Pydantic valida il contratto OpenAI; il gateway crea InferenceRequest e chiama l’applicazione. Mappa errori a status/envelope OpenAI e mantiene request ID. Per consentire fallback pre-output, lo stream bufferizza il primo evento significativo prima di inviare headers. Dopo il primo delta di contenuto/tool consegnato non è possibile fallback. LAR restituisce tool call ma non esegue strumenti: il controllo resta a OpenCode.

### Orchestration Engine
TaskAnalyzer estrae requisiti deterministicamente (tools, vision, JSON, context/output); ModelRouter filtra e confronta candidati; Policy Engine applica compatibilità/admission/limiti tentativi; Scheduler mantiene bounded FIFO e un worker; fallback è una transizione esplicita tra candidati. Router e scheduler non fanno HTTP Ollama diretto. Il routing basato su risorse avviene al dequeue, su snapshot recente.

### Resource Manager
Discovery CPU/RAM/GPU/sensori; snapshot continuo e on-demand; stima componenti/range RAM e VRAM; admission safe/constrained/rejected; simulazione hardware sintetica. Ogni sample reca timestamp, fonte, unità, freshness e qualità. Sensore assente è null, non zero.

La stima considera pesi/quantizzazione, KV cache/context, batch e sequenze, overhead runtime, offload CPU/GPU, risorse libere e processi esterni. La formula KV usa layer, KV heads, head dimension, precisione cache, contesto e sequenze solo se l’architettura è nota; modelli MLA/ibridi richiedono metadati/calibrazione. Non applicare fattori costanti senza evidenza. Safe indica stima upper bound entro budget e dati freschi; constrained indica margine/stima/offload incerti; rejected indica incompatibilità dura o insufficienza anche con stima favorevole. Nessuno garantisce assenza OOM.

### Model Intelligence
Scoperta/versionamento artifact Ollama tramite digest e quantizzazione; profili per artifact/hardware/runtime; benchmark e metriche storici; ranking/report con provenance e copertura. L’attuale ranking benchmark non basta da solo per instradare richieste.

### Model Manager
Gestisce stato osservato, ownership, load/unload, singola lease, cambio modello, idle timeout e reconcile. Errore unload lascia stato unknown/reconciling e impedisce nuovo managed load finché riconciliato. I modelli esterni non si scaricano. La lease inferenziale non coincide con il numero di modelli in memoria.

### Inference Provider
OllamaInferenceProvider è l’unico backend v1: health, discovery, inspect, elenco running, load/unload, generate e chat/stream tramite `/api/chat`. L’adapter valida chunk NDJSON e risposta finale, conserva `tool_calls` come handoff e non esegue strumenti né applica retry. La cancellazione del client chiude la richiesta HTTP ma non prova l’arresto fisico Ollama; il provider v1 non espone una conferma affidabile. L’astrazione non introduce altri backend implementati.

### Observability e Storage
Log JSON UTC con request ID, decision ID, model digest, fase, durata, errore e risorse; non includono prompt/completion/secret per default. Metriche di coda, load, first token, token, generazione e risorse sono separate dai benchmark. SQLite contiene configurazioni non segrete, catalogo/profili, benchmark, metadata richieste, decisioni, lifecycle e snapshot con schema versionato.

## Dipendenze consentite

Flusso: OpenAI client → Gateway → LARApplication → Scheduler → Analyzer/Router → Intelligence + ResourceManager + Policy → ModelManager → Provider → Ollama.

- Gateway dipende da contratti app e Pydantic/FastAPI; non importa Ollama.
- Provider contiene API Ollama e HTTP client.
- ModelManager dipende da InferenceProvider.
- ResourceManager non seleziona modelli.
- ModelIntelligence non dipende da Gateway/OpenCode.
- Scheduler non dipende da HTTP o SQL specifico.
- Storage non dipende da provider/gateway.
- OpenCode non è dipendenza Python.
- Le dipendenze già dichiarate comprendono FastAPI, Uvicorn, httpx, Pydantic, PyYAML e psutil; nessuna nuova dipendenza è presupposta.

## Flusso richiesta

1. Genera request ID, valida OpenAI; malformed è rifiutato.
2. Analyzer crea TaskProfile e ContextRequirements.
3. Accoda con sequence number; queue full/scadenza producono errore esplicito.
4. Al dequeue acquisisce snapshot corrente e catalogo/profili versionati.
5. Filtra capability/context e seleziona solo benchmark comparabili.
6. Esegue admission/policy; rejected è escluso e motivato.
7. Ordina e registra alternative/evidenze.
8. Acquisisce lease, riconcilia stato e cambia modello scaricando solo il managed precedente.
9. Avvia stream e bufferizza il primo evento significativo.
10. Se l’errore è recuperabile prima dell’output, tenta il successivo candidato ammesso non tentato entro la stessa deadline.
11. Consegna JSON/SSE; dopo un delta, completa o invia stream error senza fallback.
12. Rilascia lease e registra metriche/eventi su ogni esito, cancel, timeout e shutdown.

## Routing deterministico

Prima filtri hard per capability, context e ammissione. Poi ordinamento Pareto sulle sole metriche misurate/confrontabili: qualità benchmark e throughput massimizzati; latenza/memoria minimizzate; stabilità da run ripetuti. Efficienza energetica resta sconosciuta finché non misurata. Non introdurre pesi arbitrari. Un candidato domina se non è peggiore su ogni metrica disponibile ed è migliore almeno su una. Missing non è zero né imputato; copertura/uncertainty restano visibili. Tie-break policy versionato e stabile su model ID/digest. Se non vi sono benchmark comparabili, la decisione espone lo stato unscored, e usa solo candidati compatibili/admitted secondo policy; non dichiara vincitore misurato. Tool non supportati e contesto impossibile sono esclusioni dure.

## Fallback e lifecycle

Stati: received → validated → queued → routing → admitted → loading → pre_output → streaming → completed; terminali alternativi invalid, cancelled, queue_timeout, rejected, provider_error, load_error, generation_error, stream_error, timeout, resource_error, shutdown.

- Errori provider/load prima dell’inferenza: alternativa possibile se c’è deadline.
- Prima del primo token: fallback solo se niente contenuto/tool call è stato consegnato e risposta HTTP è recuperabile.
- Dopo delta consegnato: niente fallback, preservare errore e metriche parziali.
- Cancel, malformed, no candidate, deadline scaduta: no fallback.
- OOM: aggiornare snapshot e rivalutare candidati non tentati.
- Candidate unici, massimo un tentativo ciascuno; fallback non resetta deadline.
- Lease sempre rilasciata in cleanup; idle unload non tocca il modello richiesto/attivo.
- Shutdown blocca nuovi submit e drena o cancella queued/active secondo deadline esplicita.

## Decisioni aperte

Sono già vincolati: autonomia, solo Ollama v1, FIFO/serialità LAR, fallback limitato, routing bilanciato/tracciabile, hardware auto-discovery, benchmark reali, API OpenAI e indipendenza da OpenCode.

Da definire prima di implementare: esclusività Ollama host o solo LAR; tokenizer/context estimation e contesto marginale; riserve RAM/VRAM/soglie temperatura/freschezza da calibrare; auth/CORS e bind oltre loopback; retention/opt-in contenuti; GET models solo lar-auto o modelli concreti; drain/cancel shutdown; sensori/GPU supportati; numero minimo di run per stabilità; strategia migrazione DB.
