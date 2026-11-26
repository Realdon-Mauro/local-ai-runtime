# LAR — Gap analysis implementativa

Analisi statica del repository al 20 novembre 2026, integrata con le evidenze del ciclo di lavoro del 16 novembre 2026 e con l’audit OpenCode v2.2.7. La precedente nota sull’assenza di `.git` è superata: il repository è Git e la baseline della presente revisione è `03c994a1ced6284c6ee4664d80d507aefcd55dd6`. I 106 test superati e Ruff senza errori sono risultati riportati per il ciclo del 16 novembre, non rieseguiti in questa revisione. `~/benchmark-ai` resta fuori ambito. `results/lar.db` è stato ispezionato in sola lettura nell’analisi precedente; nessun dato operativo è modificato qui.

## Sintesi

Il repository contiene il runtime API FastAPI, il client Ollama chat/stream, router e admission iniziali, Scheduler/Model Manager, configurazione e storage runtime v1, più CLI operativa. La suite è stata eseguita localmente. Restano limiti espliciti nella correlazione osservativa OpenCode, nella persistenza della telemetria runtime e nel collegamento del router ai benchmark reali. Il blocco fail-safe tratta la terminazione fisica Ollama come ignota quando manca un verificatore provider.

## Stato per componente

| Componente | Stato | Evidenza | Discrepanza |
|---|---|---|---|
| Ollama provider | Parziale, riusabile | GenerationProvider e ChatProvider; health, tags, ps, load/unload, generate, chat e stream NDJSON `/api/chat`, mapping errori, metriche finali e tool call pass-through sono implementati/testati con MockTransport. | LAR/tool IDs e OpenAI contract sono ancora da adattare nel gateway; chiusura HTTP non prova stop fisico; provider non implementa altri backend. |
| Discovery | Parziale | evaluation/discovery.py legge digest/size/metadata/capabilities/inspection error. | Serial, dataclass mutabile, no profili runtime/hardware. |
| Catalogo | Parziale | evaluation/catalog.py scans/snapshots con FK/indici e storico. | Nessun artifact normalized identity/schema registry. |
| ModelManager | Implementato per invarianti LAR-scoped | Lock condiviso, lease UUID, ownership/presence separate, snapshot immutabili, reconciliation periodica/on-demand e gestione esito load ambiguo. | Non controlla client esterni né garantisce un modello totale residente sull’istanza condivisa; non scarica modelli esterni. |
| Scheduler | Implementato, fail-closed | FIFO bounded max 10, worker singolo, backpressure, sequence number, timeout e cleanup lease; stop ignoto persiste su DB e impedisce startup successivi finché l’operatore non registra attestazione esplicita. | La terminazione fisica Ollama su cancel/timeout non è confermabile; lo sblocco è attestazione manuale, non verifica automatica. |
| Orchestrazione/chat | Parziale | TaskAnalyzer/ModelRouter deterministici in `core/router.py`: filtri capability/context, admission, trace e Pareto su prove comparative; LARApplication integrata con Scheduler, lease e provider. Decisioni, candidati, hardware e snapshot risorsa sono persistiti minimizzando il payload. Fallback pre-inferenza su HTTP 404 del load è limitato alle alternative ammesse e tracciato. | Nessun fallback per errore ambiguo; non è collegato lo storage benchmark; tokenizer compatibile non configurato, quindi input context resta unknown/constrained. |
| API OpenAI-compatible | Implementata come subset verificato | Gateway FastAPI JSON/SSE, `/v1/models` espone solo `lar-auto`, auth bearer opzionale, CORS off di default, bind loopback. | Non è compatibilità completa; stati/decisioni non persistiti, fallback e disconnect provider stop restano limitati. |
| Resource Manager | Parziale, iniziale | `core/resources.py` misura CPU/RAM/processi/storage; NVIDIA via `nvidia-smi`; sensori termici psutil; loaded models via `/api/ps`; assenza esplicita nei `sources`. Stima KV solo con tutti i metadati richiesti; admission SAFE/CONSTRAINED/REJECTED con test. Hardware profile e snapshot pre-routing sono persistiti con le fonti. | No AMD/Apple GPU adapter, no estimation profiles/calibration, simulation/hysteresis/monitor loop e raccolta continua assenti; SAFE richiede metadata di upper bounds espliciti. |
| Model Intelligence | Parziale | evaluation esistente più interfaccia immutabile BenchmarkEvidence usata dal router solo quando digest/quantizzazione/spec/hardware/runtime/options sono compatibili. Le decisioni e i candidati sono persistiti nel DB runtime. | Lo storage benchmark non alimenta ancora questa interfaccia; i candidati restano `unscored` finché mancano evidenze comparabili; manca profilo/aggregazione e audit evidence nel DB runtime. |
| Benchmark | Implementato per use case ristretto; primo ciclo end-to-end verificato | runner/orchestrator/CLI, systemd/bwrap/limits/ro-bind/seccomp denylist. `coding.file_stats` ha prodotto un risultato effettivo per `qwen2.5-coder:7b`, persistito in SQLite: 3/10, score 0,30, throughput 53,74 token/s. Import esplicito read-only verso runtime preserva misure/provenienza disponibile, scarta output raw e marca comparabilità `unscored`. | Il singolo risultato non è valutazione generale e non dimostra comparabilità cross-hardware. I benchmark CLI scrivono ancora nel DB legacy e non nel catalogo benchmark runtime; non alimentano il router automaticamente. Sandbox tests non eseguono systemd reale e2e. Seccomp denylist con default allow, non allowlist completa. Resta cleanup processi sandbox da verificare. |
| Observability | Parziale | Request ID attraversa gateway, storage e route; JSON log minimizzato; metriche request queue wait, first stream chunk, wall total e usage Ollama con fonti/null. | model-load da lease non misurato, campionamento risorsa continuo/peak assente, misure fallite/cancellate parziali. |
| SQLite runtime | Parziale | Migration v1–v2, registry/checksum, backup verificato, integrity/FK check, CLI migrate/check/backup/prune, persistenza minimizzata ciclo richiesta, decisioni/candidati/snapshot e retention ADR-004 con audit testati su DB temporanei. | Restore controllato e metriche inferenza risorsa/durata non persistite; non importa i dati legacy; `results/lar.db` viene rifiutato. |
| Privacy | Parziale | Traffico runtime persiste solo metadati; prompt, output e tool arguments esclusi. Retention eventi/richieste e audit pruning implementati. | Benchmark legacy continua a serializzare contenuti nel proprio schema e richiede policy/limiti separati; il content opt-in non è implementato. |
| Config e CLI | Implementata base operativa | YAML/Pydantic, defaults→YAML→`LAR_*`, hash/snapshot privo di token, bind sicuro, TLS, `lar config`, `lar status`, `lar db`, `lar serve`. | TLS non copre proxy esterno; retention e restore non presenti. |
| OpenCode | Parziale, sola ricezione evento | Endpoint loopback autenticato con token dedicato, DTO allowlist, deduplica `(source,native_id)`, append observation e workflow ID LAR per evento utente; config token CLI a permessi restrittivi. | Plugin/listener/recovery, outbox durevole client, delivery retry/ack e test OpenCode reali assenti; correlazioni session/workflow restano limitate. Nessuna integrazione end-to-end. |

## Benchmark integrato

benchmarks/coding/file_stats.yaml è versione 2, categoria coding, capability completion, valutazione python_unittest; unico YAML rilevato. BenchmarkRunner genera codice dal provider, lo valuta in sandbox e raccoglie metriche; orchestrator usa lease; CLI offre scan, history, run/all/ranking.

Run conserva nome modello, id/version benchmark, category, status, score/test, metriche e JSON completo. Non conserva hardware, artifact digest, runtime version o spec digest. Ranking prende ultima versione e ultimo run valido per modello/benchmark, medie categoria, overall con pesi fornibili e throughput separato. Non basta a un router cross-hardware. Evidenza disponibile: un primo ciclo benchmark end-to-end è stato completato e salvato in SQLite; `coding.file_stats`, modello `qwen2.5-coder:7b`, 3/10 casi, score 0,30, throughput 53,74 token/s. È un risultato del benchmark specifico, non una valutazione generale del modello.

## Test osservati, non eseguiti

La suite completa corrente ha superato 235 test; Ruff e `git diff --check` sono passati. Restano assenti fallback runtime, simulation/hysteresis, OOM reale in cgroup, test Ollama reale e OpenCode e2e.

## Evidenze audit OpenCode v2.2.7

La versione del binario è stata confermata tramite esecuzione isolata; API plugin e session log sono stati esaminati. Il recovery usa il session log con cursore esclusivo `after` e `follow`; `log.synced` è un marker e non un evento terminale. Il feed globale `/api/event` non è una sorgente durevole di recovery. Non è dimostrata una correlazione nativa uno-a-uno fra inbox, messaggio assistant e terminale execution; un terminale execution può coprire più drain della stessa sessione.

La prova SQLite nel runtime plugin è rimasta inconclusiva per `EPERM` sulla socket locale e la durabilità reale sul filesystem di destinazione non è stata verificata. Queste prove non attestano il funzionamento positivo della coda durevole. Sono quindi evidenze di audit/progettazione, non componenti OpenCode implementati e verificati nel repository LAR.

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
