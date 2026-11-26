# LAR — Roadmap tecnica incrementale

Questa roadmap distingue progettazione, implementazione disponibile, implementazione parziale e verifiche ancora aperte. Le fasi descrivono lavoro futuro solo dove indicato. `~/benchmark-ai` resta fuori ambito.

## Stato di base

| Area | Stato corrente | Evidenza / limite |
|---|---|---|
| Progettazione | Completata per i confini e i requisiti v1; ADR-001–ADR-010 risultano approvate. | I documenti descrivono il target e non provano implementazione. Soglie/calibrazioni e alcune verifiche tecniche restano da svolgere secondo ADR. |
| Provider Ollama, discovery, catalogo, benchmark CLI e ranking | Inventory e benchmark CLI disponibili; chat JSON/NDJSON implementata. | Ranking runtime riceve benchmark iniettati ma non li legge ancora dallo storage benchmark; niente fallback completo. |
| Model Manager | Implementazione parziale. | Lease/lock e ownership esterna sono disponibili; mancano gli invarianti globali di un solo modello managed e inferenza attiva, reconciliation completa e coordinamento FIFO. |
| Benchmark | Un primo ciclo end-to-end è stato eseguito e salvato in SQLite. | `coding.file_stats` con `qwen2.5-coder:7b`: 3/10, score 0,30, 53,74 token/s. È una singola valutazione specifica, non una valutazione generale del modello. Cleanup sandbox in caso di terminazione fallita ancora da verificare. |
| Test e lint | Nel ciclo di lavoro del 16 novembre 2026 sono stati riportati 106 test superati e Ruff senza errori. | Non rieseguiti in questa revisione; il risultato non copre requisiti non implementati. |
| OpenCode | Audit v2.2.7 e ricezione evento LAR parziale. | Recovery session log con `after` esclusivo e `follow`; `log.synced` non terminale; `/api/event` non durevole; correlazione nativa uno-a-uno non dimostrata; plugin/listener/outbox LAR ancora assenti. |

## Priorità prossima fase

La sequenza già concordata è vincolante per l’ordine di sviluppo:

1. Stabilizzazione del Model Manager.
2. Implementazione dello Scheduler FIFO bounded.
3. Verifica degli invarianti di concorrenza LAR-scoped.
4. Successiva realizzazione del gateway API OpenAI-compatible e del routing runtime.

Queste priorità non anticipano decisioni architetturali. Gli invarianti LAR-scoped non implicano esclusività sull’istanza Ollama condivisa con client esterni.

## Fasi di lavoro

### Fase 0 — Baseline e decisioni (completata)

Decisioni ADR-001–ADR-010 approvate; repository Git disponibile e baseline della revisione `03c994a1ced6284c6ee4664d80d507aefcd55dd6`. La documentazione di architettura, requisiti, contratti, database, gap analysis e roadmap è presente. Le evidenze tecniche e i limiti OpenCode sono registrati nei documenti. Non modificare il database operativo durante le fasi successive senza seguire le ADR.

### Fase 1 — Stabilizzazione Model Manager (completata nel lavoro locale)

Ownership/presence separate, lease identificabili, snapshot immutabili, gestione degli esiti ambigui di load, reconciliation on-demand e periodica sono implementate in modifiche locali non commesse. Verifica corrente: `tests/test_model_manager.py` e `tests/test_reconciliation_service.py` nella suite completa. La condivisione Ollama resta limitata al controllo LAR-scoped secondo ADR-001.

### Fase 2 — Scheduler FIFO (implementata; verifica locale)

La FIFO bounded, il worker singolo, sequence number atomici, timeout coda/richiesta, cancel e cleanup lease sono implementati in modifiche locali non commesse. La cancellazione attiva e il timeout attendono una verifica esplicita di stop; senza prova entro 60 secondi lo scheduler entra in `provider_stop_unknown`/`blocked`, rifiuta nuove richieste e termina quelle in coda. Ollama non fornisce al momento un verificatore fisico supportato: il default quindi fallisce in sicurezza e richiede intervento operatore. `/api/ps` non viene usato come prova.

### Fase 3 — Invarianti di concorrenza LAR-scoped (verifica iniziale superata)

I test concorrenti Model Manager/Scheduler verificano FIFO, singolo worker, lease e cleanup; la suite completa è stata rieseguita. La verifica copre fake provider e non dimostra esclusività host-wide o stop fisico Ollama. Il nuovo fail-safe su cancellazione/timeout è coperto da test dedicato.

### Fase 4 — Provider Ollama chat/stream e storage/config (parziale)

Provider `/api/chat` JSON/NDJSON, config YAML/Pydantic con precedenza approvata, CLI `lar` e migration runtime SQLite v1–v2 sono implementati e testati con MockTransport/fixture temporanee. Backup verificato, persistenza minimizzata degli stati richiesta e pruning ADR-004 con audit sono disponibili; restore operativo e telemetria risorsa/decision trace sono incompleti. Il database legacy è protetto e non importato. **Uscita non ancora raggiunta.**

### Fase 5 — Resource Manager e Model Intelligence/router (parziale)

Discovery CPU/RAM/processi/storage e adapter NVIDIA, temperature disponibili e modelli Ollama loaded; stima KV su metadata completi, admission esplicita, TaskAnalyzer deterministico e Router con comparabilità rigorosa/Pareto sono implementati e unit testati. Hardware, snapshot pre-routing, decisioni e candidati sono persistiti; restano hardware adapters ulteriori, profili/calibrazione empirica, simulator e hysteresis, collegamento benchmark reali al router e fallback runtime. Soglie host-specific restano configurabili.

### Fase 6 — Gateway API OpenAI-compatible e routing runtime (parziale)

FastAPI lifecycle, health/readiness, `/v1/models` con solo `lar-auto` e `/v1/chat/completions` JSON/SSE, errori/ID/tool handoff e buffering pre-output sono implementati e testati. La richiesta passa per router, Scheduler, lease Model Manager e provider. Sono persistiti stati richiesta e decisioni/candidati/snapshot; il fallback è limitato al load HTTP 404 pre-inferenza. Adapter benchmark-storage→router e fallback più ampio restano incompleti. **Uscita non ancora raggiunta.**

### Fase 7 — Observability, integrazione OpenCode e hardening (da realizzare)

La CLI diagnostica e i log minimizzati sono implementati; decisioni, candidati e snapshot risorsa pre-routing sono persistiti. Metriche richiesta base (attesa, primo chunk, durata e usage Ollama) sono collegate; model-load da lease, peak e campioni periodici non sono raccolti. È presente la ricezione loopback autenticata di singole osservazioni OpenCode; plugin, listener, recovery, outbox client e delivery at-least-once non ci sono. Restano test reali per systemd/bwrap/seccomp/OOM/cancel/recovery. **Uscita non raggiunta.**

## Verifiche tecniche aperte

- Stabilizzazione del Model Manager, Scheduler FIFO e invarianti di concorrenza LAR-scoped.
- Cleanup dei processi sandbox dopo terminazione fallita e rischio di processi orfani.
- Comportamento SQLite/WAL e recovery del plugin OpenCode nel runtime e sul filesystem di destinazione; la prova corrente è inconclusiva.
- Campi e identificativi nativi, correlazione e semantica dei terminali OpenCode; nessuna relazione uno-a-uno è dimostrata.
- Durabilità LAR, migration/recovery DB, metriche e resource admission, Ollama streaming, gateway e routing runtime.

Nessuna fase è considerata conclusa per la sola presenza di contratti o documentazione. Le fasi si chiudono solo con evidenze registrate e verifiche effettivamente eseguite. Non cambiare ordine o priorità senza motivazione documentata e approvazione delle decisioni interessate.
