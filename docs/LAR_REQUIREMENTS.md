# LAR — Requisiti e criteri di accettazione

Riferimento: LAR_ARCHITECTURE.md. P0 = necessario a v1; P1 = qualità operativa v1; P2 = follow-up. Criteri futuri, non test superati.

## Requisiti funzionali

| ID | Pri | Requisito | Criterio verificabile |
|---|---|---|---|
| FR-APP-01 | P0 | Lifecycle applicativo. | Test start/close verifica risorse aperte/chiuse una volta, submit rifiutati in shutdown e cleanup. |
| FR-API-01 | P0 | OpenAI Chat Completions. | Client standard usa POST /v1/chat/completions con lar-auto e riceve schema compatibile/request ID. |
| FR-API-02 | P0 | JSON e SSE. | Chunk ordinati, [DONE] una volta, usage se richiesto; disconnect cancella richiesta e lease. |
| FR-API-03 | P0 | Tool calling round-trip. | Schema tools arriva al provider, tool deltas preservano ID/name/arguments. LAR non esegue tool. |
| FR-API-04 | P0 | Modello virtuale. | GET /v1/models elenca lar-auto; nomi non supportati hanno rifiuto esplicito. |
| FR-API-05 | P0 | Error mapping/ID. | Test copre malformed, coda piena, Ollama down, risorse/context, timeout e stream error con request ID. |
| FR-ROUTE-01 | P0 | Analyzer deterministico senza LLM. | Input normalizzato e analyzer version uguali → TaskProfile uguale. |
| FR-ROUTE-02 | P0 | Filtri capability/context/hardware. | Tool/vision mancanti e context insufficiente non sono selezionati; reason code presente. |
| FR-ROUTE-03 | P0 | Decision trace e alternative. | Versioni, candidati/evidenze/snapshot, motivazioni e ordine alternative persistiti e riproducibili. |
| FR-ROUTE-04 | P0 | Bilanciamento senza pesi arbitrari. | Pareto usa metriche comparabili; missing non è zero; tie-break stabile. |
| FR-ROUTE-05 | P1 | Comparabilità benchmark. | Spec/digest/quantizzazione/runtime/hardware incompatibili non vengono aggregati. |
| FR-QUEUE-01 | P0 | FIFO bounded e una inferenza LAR. | Event/Barrier prova FIFO e massimo active=1; capacity superata genera errore chiaro. |
| FR-QUEUE-02 | P0 | Cancel/timeout. | Cancel queued non chiama provider; active cancel chiude stream, rilascia lease e termina cleanup. |
| FR-QUEUE-03 | P0 | Shutdown/drain. | Nuovi submit bloccati; queued drenate o cancellate secondo policy; active termina o viene cancellata entro deadline. |
| FR-MODEL-01 | P0 | Un modello LAR-managed resident. | Model switch riconcilia, unloada solo modello managed e previene duplicate loads concorrenti. |
| FR-MODEL-02 | P0 | Una lease per inferenza. | Success/error/timeout/cancel/fallback rilasciano lease; unload con lease rifiutato. |
| FR-MODEL-03 | P0 | Ownership esterna. | Modello esterno è marcato external, non scaricato da LAR; limite di controllo visibile. |
| FR-MODEL-04 | P1 | Idle timeout. | Non unloada modello active/atteso; errore unload blocca nuovo load finché reconcile. |
| FR-FALL-01 | P0 | Fallback automatico limitato. | Errore pre-output/load prova successivo candidate ammesso entro deadline; malformed/cancel non ritentano. |
| FR-FALL-02 | P0 | No fallback post-output. | Dopo content/tool delta, errore stream non avvia altra generation. |
| FR-FALL-03 | P0 | No cicli/duplicati. | Ogni artifact tentato una volta, massimo numero candidate unici/cap policy. |
| FR-RES-01 | P0 | Discovery hardware con provenance. | Fixture CPU/RAM/GPU/sensore assente produce source/timestamp/null, mai zero inventati. |
| FR-RES-02 | P0 | Monitoring e snapshot admission. | Fake clock verifica freshness/stale/processi esterni e snapshot collegato alla decisione. |
| FR-RES-03 | P0 | Resource estimate. | Test pesi, quantizzazione, KV, context, batch, overhead, offload; missing metadata riduce confidence. |
| FR-RES-04 | P0 | Admission safe/constrained/rejected. | Range/budget test produce stato/reason coerenti; nessun livello è garanzia. |
| FR-RES-05 | P1 | Simulation. | Profilo sintetico usa stesso estimator/policy, è marcato simulated e non chiama provider/cambia state. |
| FR-MI-01 | P0 | Model catalog versionato. | Refresh conserva history; digest cambiato è nuovo artifact; inspection error persiste. |
| FR-MI-02 | P0 | Benchmark versionato e separato. | Run con hardware/version/spec differenti restano distinguibili da request metrics. |
| FR-MI-03 | P1 | Report con provenance. | Report riporta fonte, hardware/runtime/benchmark/version/coprertura; missing non inventato. |
| FR-OBS-01 | P0 | Log e request ID. | ID collega gateway/queue/route/lifecycle/provider/storage; raw prompt/completion assenti per default. |
| FR-OBS-02 | P1 | Metriche runtime. | Queue/load/first token/tokens/resources con fonte/null; benchmark separato. |
| FR-STO-01 | P0 | SQLite versionato. | Nuovo DB e legacy fixture migrano; FK/integrity/version registry verificati. |
| FR-STO-02 | P0 | Storico append-only. | Nuovo catalog/decision non sovrascrive vecchio; retention documentata. |
| FR-STO-03 | P0 | No contenuto richiesta di default. | Canary prompt/completion assente da DB/log; eventuale opt-in separato. |
| FR-OLL-01 | P0 | Solo Ollama v1, API isolate. | Solo adapter importa/chiama API Ollama; fake provider consente unit test. |

## Requisiti non funzionali

| ID | Pri | Requisito | Criterio verificabile |
|---|---|---|---|
| NFR-DET-01 | P0 | Routing riproducibile. | Stessi input/snapshot/versioni generano primary, alternatives e reasons uguali. |
| NFR-REL-01 | P0 | Cleanup su ogni errore. | Fault/cancel non lascia task, lock, lease o connessioni. |
| NFR-RES-01 | P0 | Freshness visibile. | Snapshot stale degrada la decisione e non è spacciato per attuale. |
| NFR-SEC-01 | P0 | Loopback e minimizzazione. | Default bind 127.0.0.1; bind pubblico richiede protezione esplicita. |
| NFR-SEC-02 | P0 | Sandbox benchmark. | Test system Linux prova read-only, isolamento rete, limiti e cleanup. |
| NFR-CMP-01 | P0 | Limite accesso Ollama esterno esplicito. | Health/trace segnala modelli external; serialità dichiarata LAR-scoped. |
| NFR-DB-01 | P0 | Migration recoverable. | Upgrade/rollback/restore fixture preserva conteggi e IDs. |
| NFR-UX-01 | P1 | Backpressure/errori correlabili. | Client riceve status, error code, request ID e retry behavior. |
| NFR-PERF-01 | P1 | No SLO inventati. | Soglie solo dopo benchmark baseline reale. |
| NFR-MAINT-01 | P0 | Confini modulari senza microservizi/backend extra. | Provider/storage/API si testano con fake; no backend non Ollama. |

## Piano test futuro

**Unit:** Pareto/tie-break/missing/comparability, resource formulas/ranges/admission, lease/ownership/load/unload, fallback matrix/no retry after output, OpenAI DTO/tool deltas/error mapping, serialization/data origin.

**Integration:** MockTransport Ollama health/discovery/chat/stream/load/unload/HTTP/timeout/disconnect; SQLite temporary init/migration/rollback/FK/privacy canary; queue+manager fake provider FIFO/cancel/fallback; hardware adapters fake e simulazione; ASGI JSON/SSE/tool/usage/errors.

**System:** systemd user, Bubblewrap e seccomp reali; timeout/OOM in cgroup isolato, cancel/cleanup; LAR/Ollama restart, provider/model/unload failures, SQLite recovery; model switch ed external model. Client esterno concurrent deve dimostrare il limite, non nasconderlo.

**Hardware:** CPU/RAM/GPU/VRAM/temperature/process discovery con unit/source/time; sensore/driver unavailable degrada; pressione solo isolata; simulator fixture include i5-12400F, RTX 4060 8 GB, 16 GB RAM senza confondere simulation/reality; confronti non compatibili rifiutati.

**E2E OpenCode:** profilo temporaneo OpenAI-compatible su loopback/lar-auto; JSON, SSE, tool handoff e OpenCode execution; IDs/errors/FIFO/cancel; fallback pre-output e no-fallback post-delta; prompt sintetici e log senza raw content.

Stato attuale: test letti ma non eseguiti; nessun risultato dichiarato.
