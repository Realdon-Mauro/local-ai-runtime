# LAR — Registro delle decisioni architetturali

**Stato:** dieci ADR (ADR-001–ADR-010) sono approvate esplicitamente.
**Ambito:** prima versione locale di LAR con Ollama, API compatibile OpenAI e SQLite.
**Riferimenti:** [architettura](LAR_ARCHITECTURE.md), [requisiti](LAR_REQUIREMENTS.md), [contratti](LAR_CONTRACTS.md), [database](LAR_DATABASE.md), [gap analysis](LAR_IMPLEMENTATION_GAP_ANALYSIS.md), [roadmap](LAR_ROADMAP.md).

## Sintesi delle decisioni proposte

| ID | Tema | Raccomandazione | Classe proposta | Stato |
|---|---|---|---|---|
| ADR-001 | Client Ollama esterni e ownership | Istanza condivisa; LAR serializza solo il proprio scheduler; ownership indica l’origine del caricamento; niente unload amministrativi in modalità condivisa | Obbligatoria in v1; modalità dedicata fuori ambito | **APPROVATA** |
| ADR-002 | Stima dei token e del contesto | Strategia ibrida: tokenizer esatto solo con compatibilità verificata, altrimenti stima calibrata o prudenziale; preservare provenienza e incertezza; conteggi Ollama separati | Obbligatoria; adapter/configurazione rimandabili | **APPROVATA** |
| ADR-003 | Admission e risorse | Stati SAFE/CONSTRAINED/REJECTED; in CONSTRAINED alternativa compatibile più leggera, poi misure conservative monitorate; fedeltà della richiesta | Semantica obbligatoria; parametri host-specific configurabili | **APPROVATA** |
| ADR-004 | Retention | Retention differenziata per classe; contenuti disattivati per default; benchmark e provenienza preservati | Classi obbligatorie; durate configurabili e versionate | **APPROVATA** |
| ADR-005 | Confine API e sicurezza locale | Bind loopback per default; auth locale configurabile ma non obbligatoria nel profilo single-user; qualsiasi binding non-loopback richiede auth e TLS; CORS off; solo `lar-auto` esposto | Default obbligatorio; esposizione remota configurabile | **APPROVATA — A** |
| ADR-006 | Shutdown e timeout | Stop-submit; periodo di grazia per il drain; poi cancellazione best-effort, completamento LAR e cleanup garantito della lease | Semantica obbligatoria; timeout, grace period e comportamenti configurabili | **APPROVATA — A** |
| ADR-007 | Evidenza benchmark | Comparabilità esplicita e deterministica; solo risultati comparable entrano nel ranking; condizioni incompatibili sono not_comparable e dati insufficienti/inaffidabili unscored | Provenienza e tracciabilità obbligatorie; calibrazione statistica in Fase 1 | **APPROVATA — A** |
| ADR-008 | Configurazione, migrazioni SQLite e dati legacy | Config YAML esterna con schema versionato e validazione Pydantic; precedence defaults → YAML → environment; migrazioni conservative; importazione controllata verso un nuovo database LAR; snapshot/hash effettivo senza segreti | Quattro decisioni approvate; dettagli operativi indicati come attività di Fase 1 | **APPROVATA — A** |
| ADR-009 | Alias e discovery esposti | Pubblicare esclusivamente `lar-auto`; discovery, catalogo e selezione deterministica dei modelli fisici restano interni a LAR | Obbligatoria in v1; API amministrative future e secondo router esclusi | **APPROVATA — A** |
| ADR-010 | Integrazione OpenCode e consegna durevole degli eventi | Integrazione osservativa per OpenCode v2.2.7; correlazione rigorosa e separata; coda con priorità, consegna at-least-once idempotente, privacy per default e recovery tramite session log | Decisioni approvate; funzionamento runtime SQLite e dettagli tecnici ancora da verificare | **APPROVATA** |

### Riepilogo delle decisioni approvate

Sono approvate ADR-001–ADR-010. ADR-006 approva l’opzione A (cancellazione cooperativa): stop-submit allo shutdown, drain con periodo di grazia configurabile, cancellazione best-effort delle richieste rimanenti e cleanup garantito della lease. La lease governa l’occupazione logica LAR; la terminazione fisica Ollama resta non confermata finché il provider non la conferma esplicitamente. Gli stati e la telemetria conservano tale distinzione. ADR-007 approva l’opzione A (comparabilità rigorosa): il ranking usa esclusivamente risultati dichiarati comparable; incompatibilità comportano `not_comparable`, mentre evidenza insufficiente o inaffidabile comporta `unscored`, senza penalità. I benchmark sono una fonte separata dalle metriche delle inferenze operative. La comparabilità e le relative motivazioni sono valutate e tracciate secondo regole documentate e deterministiche; risultati originali e provenienza sono conservati. ADR-010 formalizza l’integrazione osservativa OpenCode-LAR, la coda durevole del plugin e le garanzie di consegna senza attribuire a OpenCode o ai suoi stream completezza che non è verificabile.

ADR-008 è approvata nel complesso con quattro scelte. (1) YAML è il formato principale: schema versionato, validazione Pydantic e gestione esplicita delle incompatibilità rendono la configurazione verificabile e impediscono interpretazioni silenziose; i segreti non entrano nei file versionati. (2) La precedenza è defaults interni → YAML → environment: consente override espliciti per ambiente mantenendo determinismo; variabili non impostate non sovrascrivono YAML e valori presenti ma non validi causano errore. Lo snapshot/hash effettivo non contiene segreti. (3) Sono approvate migrazioni conservative: numerate, ordinate e versionate, con backup preventivo, transazione quando supportata, registro persistente, verifica d’integrità e protezione dalla doppia applicazione. Rollback e idempotenza sono richiesti nei limiti delle garanzie SQLite, senza presumere rollback dove non è supportato; questo riduce il rischio di perdita e rende ricostruibile lo stato. (4) È approvata l’importazione controllata dei dati legacy: `results/lar.db` resta intatto; i dati si importano solo su un nuovo database con schema LAR tramite procedura dedicata ed esplicita. Provenienza, identificativi originali o relativa mappatura, stati `legacy_unknown`, gestione visibile dei dati incompatibili, ripetibilità senza duplicazioni e riepilogo degli esiti sono vincoli dell’importazione. Il confronto fra originale e risultato deve restare possibile e non si presume convertibilità universale. Questa scelta conserva la fonte e rende auditabili conversioni e limiti senza bloccare l’approvazione sui dettagli tecnici, rimandati alla Fase 1.

ADR-009 approva l’opzione A (alias unico): l’unico modello esposto ai client è `lar-auto`, restituito da `/v1/models` e richiesto per ogni inferenza. L’alias è indipendente dal modello fisico scelto dal router; i client non possono selezionare modelli Ollama fisici via API pubblica. LAR è responsabile della discovery interna, con Ollama come provider della prima versione, e mantiene un catalogo interno con disponibilità, caratteristiche, benchmark e compatibilità. Il router seleziona deterministicamente il modello fisico più adatto in base alle policy e alle informazioni disponibili. Discovery e catalogo non dipendono dall’elenco pubblico; i modelli fisici restano identificabili e gestibili internamente. In v1 non si aggiunge un endpoint amministrativo per esporre il catalogo; eventuali API amministrative future richiedono una decisione architetturale separata. Non si introduce un secondo router né un secondo agente LLM per selezionare i modelli. ADR-010 approva le regole di correlazione, overflow, recovery e minimizzazione riportate nelle sezioni seguenti; ciò non equivale a verifica runtime dell’implementazione.

## Evidenze e limiti della verifica

La verifica è stata eseguita su `~/local-ai-runtime`. All’inizio il working tree era pulito e `git log -1 --oneline` restituiva `03c994a chore: initialize LAR repository`. I sei documenti di riferimento sono presenti. La documentazione di gap e roadmap riflette l’analisi precedente all’inizializzazione Git: i passaggi che segnalano l’assenza di `.git` descrivono quello stato storico, non quello corrente. Non sono stati modificati.

Dall’ispezione statica del codice e dei test emerge quanto segue:

- Il progetto implementa soprattutto discovery e benchmark CLI. `src/lar/providers/ollama.py` usa endpoint Ollama per version, tags, ps, show e generazione non-streaming via `/api/generate`; il protocollo provider corrente non espone chat streaming o tool calling.
- `src/lar/core/model_manager.py` protegge operazioni con lock e offre lease; distingue modelli preesistenti tramite ownership. Non implementa lo scheduler FIFO globale né dimostra il vincolo di una sola inferenza attiva; il manager può tenere stato di più modelli.
- Non risultano implementati il gateway HTTP/SSE, il router runtime, l’admission control o il monitoraggio continuo descritti nella specifica. I contratti sono proposte, non prova di implementazione.
- `config/` non contiene configurazione operativa. `pyproject.toml` dichiara già dipendenze come FastAPI, Uvicorn, HTTPX, Pydantic, PyYAML e psutil, ma ciò non dimostra che i relativi sottosistemi siano attivi.
- La lettura statica dei test non equivale alla loro esecuzione; in questa Fase 0 non sono stati eseguiti test.
- Il database `results/lar.db` è stato documentato come legacy da preservare. Non è stato modificato.

Il client Ollama installato rispondeva con versione `0.40.11`, ma i controlli del servizio e di `127.0.0.1:11434` non sono stati possibili in questo ambiente: l’accesso a systemd e al socket loopback è stato negato/non disponibile. Non è quindi verificato se Ollama sia attivo, quali modelli siano caricati o quali metriche esponga sul sistema reale.

Le fonti ufficiali Ollama consultate descrivono `/api/ps` come inventario dei modelli residenti e i conteggi di token nei risultati finali di generazione/chat; la FAQ descrive parallelismo, coda e limiti configurabili del server. Queste proprietà sono del provider e possono variare con la versione installata. Si veda [API ps](https://docs.ollama.com/api/ps), [API generate](https://docs.ollama.com/api/generate), [API chat](https://docs.ollama.com/api/chat), [FAQ locale](https://docs.ollama.com/faq) e [context length](https://docs.ollama.com/context-length). Verificare capacità e campi contro la versione Ollama effettivamente supportata prima dell’implementazione. Le fonti non dimostrano che LAR possa attribuire le richieste attive ai client esterni.

## ADR-001 — Uso concorrente di Ollama da client esterni

### 1. Problema

LAR può serializzare il proprio lavoro, ma Ollama è un servizio locale condivisibile. Client esterni possono inviare inferenze, caricare modelli o consumare memoria senza passare dallo scheduler LAR. L’inventario dei modelli residenti non prova che nessun client li stia usando. Un unload basato su una sola osservazione può interrompere o compromettere richieste esterne. La configurazione di Ollama può inoltre consentire più modelli o richieste in parallelo.

### 2. Alternative considerate

1. **Ollama condiviso con coordinamento LAR-scoped.** LAR governa solo le proprie richieste; considera stato e risorse esterne come osservazioni parziali.
2. **Esclusività logica tramite convenzione.** Chiedere agli altri client di non usare l’istanza durante LAR.
3. **Istanza dedicata e controllata.** Separare endpoint/processo e accesso OS, eventualmente limitando la concorrenza nella configurazione del servizio.
4. **Controllo esclusivo globale presunto.** Trattare la presenza di LAR come prova che nessun altro client usa Ollama.

### 3. Vantaggi e svantaggi

- L’opzione 1 non pretende controllo oltre i confini di LAR, resta compatibile con l’installazione corrente e consente di reagire alla pressione osservata. Non garantisce che il totale degli utilizzi sia seriale, né che le misure siano prive di race.
- L’opzione 2 è semplice ma non tecnicamente verificabile né applicabile a client non cooperanti.
- L’opzione 3 offre isolamento operativo maggiore, ma richiede deployment e configurazione amministrativa, più risorse e una verifica esplicita dei confini OS. Una porta loopback da sola non costituisce controllo esclusivo.
- L’opzione 4 è semplice ma falsa in un ambiente condiviso e rende insicuri unload e admission.

### 4. Decisione approvata e impatto sull’architettura

**Stato: APPROVATA.** Ollama è condiviso nella prima versione di LAR. LAR controlla e serializza esclusivamente le richieste che transitano attraverso il proprio scheduler; non garantisce la serializzazione globale delle richieste Ollama. Gli invarianti di concorrenza di LAR sono quindi LAR-scoped.

Ownership indica chi ha originato il caricamento del modello, non chi lo utilizza in esclusiva. Un modello caricato da LAR può essere usato anche da client esterni. I modelli di provenienza esterna o sconosciuta non devono essere scaricati da LAR. In modalità condivisa LAR non esegue unload amministrativi espliciti: rilascia le proprie lease e lascia a Ollama la gestione della permanenza in memoria tramite `keep_alive` e i propri meccanismi di eviction.

`/api/ps` è un’osservazione dei modelli residenti e non si presume che identifichi tutti i client o le richieste attive. Le osservazioni di stato esterne restano parziali; LAR non deduce da esse un uso esclusivo o l’assenza di richieste esterne. Un’eventuale modalità Ollama dedicata non rientra nella prima versione e richiederà una decisione architetturale separata.

### 5. Impatto sui contratti Python

`InferenceProvider.list_running()` restituisce osservazioni provider, senza attribuire in modo inventato il client owner. `ProviderModelState` deve esprimere `ownership = lar | external | unknown`, provenienza/ora dell’osservazione e campi non disponibili come `None`. `ModelManager.reconcile()` deve evitare unload per `external`/`unknown`; lease LAR non equivale a lock globale Ollama. Le eccezioni di conflitto esterno devono essere distinguibili da provider non disponibile. Test richiesti: modello già residente, carico esterno apparso prima/dopo admission, stato obsoleto, unload non invocato su ownership incerta e limiti dell’istanza dedicata.

### 6. Impatto sul database

Persistire eventi di lifecycle con ownership, origine dell’osservazione, timestamp e digest nullable. Separare “osservato residente” da “gestito/caricato da LAR”. Una misura di pressione esterna resta una misura con fonte e non una lista completa di richieste. Nessun record legacy deve essere reinterpretato come ownership certa.

### 7. Motivazioni

La condivisione rispecchia l’uso di un servizio Ollama locale che può ricevere richieste da client diversi e mantiene i controlli di LAR entro il confine che può governare: il proprio scheduler. La serializzazione globale non è verificabile né garantibile da LAR. Separare l’origine del caricamento dall’uso evita di trattare ownership come esclusività e protegge i modelli esterni o di origine ignota da scaricamenti impropri. Senza visibilità completa su client e richieste, l’unload amministrativo in modalità condivisa potrebbe interferire con uso legittimo; il lifecycle della memoria resta quindi a Ollama.

### 8. Decisione

**APPROVATA:** Ollama condiviso in v1; controllo e serializzazione limitati alle richieste passate dallo scheduler LAR; nessuna garanzia di serializzazione globale; ownership come origine del caricamento, senza esclusività d’uso; nessun unload di modelli esterni/ignoti e nessun unload amministrativo esplicito in modalità condivisa; rilascio delle lease LAR e gestione della permanenza a Ollama tramite `keep_alive`/eviction; nessuna attribuzione completa di client o richieste tramite `/api/ps`. Un’eventuale modalità dedicata richiede una ADR separata e non fa parte di v1.

## ADR-002 — Stima dei token, contesto e KV cache

**Stato: APPROVATA.** LAR adotta una strategia ibrida per stimare i token. Usa un tokenizer esatto soltanto quando può verificare la compatibilità con modello, digest/versione, template e trasformazioni della richiesta. Se non è disponibile un tokenizer compatibile, usa una stima calibrata solo quando esistono dati sufficienti e pertinenti; se la calibrazione è insufficiente può ricorrere a una stima prudenziale, marcandola con confidenza ridotta. In assenza di informazioni sufficienti il valore è `None`/non disponibile, mai zero.

Ogni stima conserva, quando disponibili, provenienza, metodo, versione, assunzioni, intervallo e confidenza. La precisione numerica non equivale all’affidabilità: valori uguali possono avere provenienza e confidenza differenti. I conteggi effettivi restituiti da Ollama sono registrati separatamente dalle stime preventive. La stima della memoria considera KV cache, contesto, architettura del modello, quantizzazione, sequenze concorrenti lato provider, offload e overhead runtime. Il limite di output è configurabile quando il client non lo specifica. Una stima euristica non è mai presentata come misura esatta o garanzia di sostenibilità.

### 1. Problema

Il router deve decidere se una richiesta entra nel contesto e nelle risorse, prima di generarla. Tokenizer, template chat, serializzazione dei tool e versioni dei modelli variano; i conteggi forniti al termine da Ollama non sono disponibili per l’admission preventiva. Il contesto configurato determina la KV cache e può incidere sensibilmente sulla VRAM/RAM.

### 2. Alternative considerate

1. Contare esattamente con tokenizer per modello e template compatibili.
2. Stimare usando un adapter tokenizer noto, oppure un modello calibrato su conteggi osservati per digest/template.
3. Usare una euristica generale (per esempio caratteri/byte per token) come misura esatta.
4. Non stimare; affidarsi al rifiuto del provider dopo l’avvio.

### 3. Vantaggi e svantaggi

- L’opzione 1 è precisa solo se tokenizer, versione, template e trasformazioni della richiesta corrispondono esattamente al provider; distribuzione o download tokenizer può introdurre dipendenze e costi.
- L’opzione 2 fornisce intervalli e confidenza, può migliorare con risultati reali, ma resta una stima dipendente da modello e distribuzione dei dati.
- L’opzione 3 è semplice, ma lingue, codice, tool schema e formattazione possono renderla errata; non può essere presentata come esatta né usare bound non convalidati per una garanzia di sicurezza.
- L’opzione 4 evita falsa precisione ma espone a OOM e fallimenti tardivi.

### 4. Impatto sull’architettura

Separare esplicitamente:

- **Misurato:** `prompt_eval_count` e `eval_count` restituiti dal provider a inferenza completata; contesto/runtime osservati dopo il load ove disponibili. Sono misure provider per quella richiesta, con versione/runtime e fase.
- **Dichiarato:** context limit, metadata e quantizzazione dichiarati da artefatto/provider. Non equivalgono al contesto effettivamente allocato o utilizzato.
- **Stimato:** preflight dei token, output, KV cache, overhead e memoria. Conservare metodo/versione, bounds, assunzioni, campioni e confidenza.
- **Non disponibile:** `None` con origine/causa assente; mai convertire in zero.

Per input, usare tokenizer esatto solo quando modello, digest/versione, template e trasformazioni della richiesta sono verificabilmente compatibili. Altrimenti usare una stima calibrata per dati sufficienti e pertinenti, con bound conservativo e copertura espliciti; se la calibrazione è insufficiente è ammessa una stima prudenziale a confidenza ridotta. Se le informazioni non bastano, il valore è `None`, mai zero. Per output usare il limite richiesto dal client; se assente, applicare un limite operativo configurabile e versionato invece di assumere output illimitato. Richiesto contesto = limite superiore input + output riservato + framing/tool overhead noto. Superare il limite dichiarato del modello/runtime è incompatibilità; un bound incerto non consente di dichiarare SAFE.

La stima della memoria deve considerare almeno KV cache, lunghezza contesto effettiva/configurata, architettura del modello (layer, KV heads, dimensioni e tipo cache), quantizzazione, sequenze concorrenti lato provider, offload e overhead runtime. Se i parametri mancano, restituire un intervallo ampio/unknown e ridurre il livello di ammissione. `num_ctx` effettivo osservato via provider è misura; il valore voluto è configurazione; il fabbisogno pre-load è stima.

### 5. Impatto sui contratti Python

Estendere/precisare `ContextRequirements`, `ResourceComponent` e `ResourceEstimate` con origine, metodo/versione, bounds, confidenza, tokenizer/template ID e cause di dato mancante. I valori misurati post-run appartengono a `InferenceMetrics`, non vanno sovrascritti nella stima iniziale. `TaskAnalyzer` deve rimanere deterministico e non dipendere da un secondo LLM. `InferenceProvider` mappa i contatori finali disponibili. Nessun contratto promette esattezza per un adapter euristico.

### 6. Impatto sul database

Salvare stima pre-run e misure post-run in campi/tabelle distinti, con metodo/versione e riferimenti al digest/provider. Persistire i dati minimi per calibrazione aggregata senza prompt/contenuti. Non mescolare metriche delle richieste ordinarie con run benchmark. Aggiornare le migrazioni solo dopo aver definito schema e provenienza.

### 7. Decisione approvata e motivazione

Adottare la strategia ibrida descritta: tokenizer esatto solo con compatibilità verificabile; stima calibrata con dati pertinenti sufficienti; stima prudenziale a confidenza ridotta quando la calibrazione è insufficiente; valore non disponibile quando le informazioni non bastano. La precisione numerica non implica affidabilità e l’euristica non costituisce misura esatta né garanzia di sostenibilità. I conteggi finali Ollama misurano il consuntivo e restano distinti dalla previsione. Se non c’è un bound difendibile, classificare CONSTRAINED o rifiutare secondo policy esplicita.

### 8. Decisione

**APPROVATA:** strategia ibrida di stima; tokenizer esatto soltanto con compatibilità verificata rispetto a modello, digest/versione, template e trasformazioni della richiesta; stima calibrata solo con dati sufficienti e pertinenti; stima prudenziale a confidenza ridotta se la calibrazione è insufficiente; `None`/non disponibile, mai zero, quando mancano informazioni; conservazione di provenienza, metodo, versione, assunzioni, intervallo e confidenza quando disponibili; separazione tra stime preventive e conteggi effettivi Ollama; stima memoria comprensiva di KV cache, contesto, architettura, quantizzazione, sequenze concorrenti provider, offload e overhead runtime; limite output configurabile se assente dal client; nessuna euristica presentata come misura esatta o garanzia di sostenibilità. La precisione numerica non equivale all’affidabilità. La scelta degli adapter e dei valori operativi resta configurabile e da calibrare con evidenza rappresentativa.

## ADR-003 — Policy risorse e stati di admission

**Stato: APPROVATA — policy CONSTRAINED: B (Bilanciata).**

### 1. Problema

Il solo peso del modello non rappresenta la memoria necessaria. Inferenza richiede KV cache e runtime; offload ripartisce RAM/VRAM. Altri processi possono consumare risorse, le letture dei sensori possono essere mancanti o obsolete e la temperatura limite varia per hardware.

### 2. Alternative considerate

1. Soglie fisse universali basate sulla configurazione di riferimento.
2. Budget per host configurabili/versionati, riserve e soglie di recovery con isteresi.
3. Nessun admission control; delegare al sistema operativo/Ollama e recuperare dopo OOM.

### 3. Vantaggi e svantaggi

- Le soglie fisse sono semplici ma non si trasferiscono tra GPU, CPU, chassis, sensori e condizioni ambientali; causano rifiuti o rischi impropri.
- Budget per host riflettono la macchina effettiva e permettono politiche verificabili; richiedono discovery, qualità dei dati e taratura.
- Delegare al provider riduce codice iniziale ma non previene OOM, thrashing o degrado e non fornisce decisioni tracciabili.

### 4. Impatto sull’architettura

Stimare almeno pesi (metadata/dimensione e quantizzazione), memoria già residente, KV cache, batch/concorrenza provider, overhead runtime, frazione offload, RAM/VRAM libera e processi esterni, CPU/capacità e carico, temperature, spazio per cache/modelli e storage LAR. Trattare la VRAM di altri processi come già occupata; includere memoria disponibile OS, non soltanto RAM totale. Separare osservazioni misurate da dichiarazioni, stime e simulazioni.

Definizioni proposte:

- **SAFE:** la richiesta è ammissibile secondo stime, margini e qualità delle informazioni disponibili. Non costituisce una garanzia assoluta.
- **CONSTRAINED:** la richiesta può essere sostenibile, ma persistono incertezze o pressione sulle risorse. LAR cerca automaticamente, se disponibile, un modello alternativo più leggero e compatibile. Se la richiesta resta ammissibile, LAR può applicare limiti interni e misure conservative e mantiene il monitoraggio durante l’inferenza.
- **REJECTED:** incompatibilità certa o condizioni che rendono l’esecuzione non ammissibile secondo la policy.

L’autonomia di LAR non deve compromettere la fedeltà della richiesta del client. LAR non taglia silenziosamente il prompt, non riduce il contesto necessario e non modifica il limite di output richiesto dal client. Se la richiesta non è compatibile con i limiti disponibili, cerca un’alternativa oppure la rifiuta esplicitamente.

Budget e margini di RAM, VRAM, CPU e spazio disco sono configurabili e specifici dell’host. Anche le soglie termiche sono configurabili e coerenti con l’hardware, senza valori universali imposti. La policy definisce freshness delle misure, isteresi, soglie di warning/trip/recovery e condizioni di recupero; i valori numerici vanno calibrati per host.

Dati mancanti o obsoleti sono rappresentati esplicitamente e mai convertiti in zero. La simulazione hardware resta separata dalle misure reali e non modifica il comportamento dell’hardware effettivo. Ogni decisione di admission conserva motivazioni, snapshot utilizzato, versione della policy e margini considerati.

### 5. Impatto sui contratti Python

`ResourceSnapshot` deve includere timestamp/freshness, fonti e qualità per sensori; `None` non diventa zero. `ResourceEstimate` mantiene componenti, bounds, assumptions, offload e versione formula. `AdmissionDecision` riporta livello, motivi, policy version e margini. `ResourceManager.simulate()` usa esattamente le formule e soglie della versione indicata ma non tocca provider, lease o macchina sintetica reale. Le decisioni di routing devono riferire snapshot e policy utilizzati.

### 6. Impatto sul database

Configurazioni di policy immutabili/versionate o snapshot con hash; persistere snapshot campionati e decisioni con FK/versioni. Campioni grezzi ad alta frequenza separati da snapshot/admission e aggregabili con retention. Non archiviare simulazioni come misure hardware: `origin=simulated` e profilo sintetico sono obbligatori.

### 7. Raccomandazione motivata

Adottare l’alternativa 2. È coerente con hardware discovery, audit e simulazione; riduce il rischio di soglie generiche. SAFE richiede margine anche sul bound superiore e sensori sufficienti, altrimenti l’incertezza emerge come CONSTRAINED/REJECTED secondo configurazione.

### 8. Decisione approvata

**APPROVATA — B (Bilanciata):** valgono le definizioni e i vincoli sopra riportati per SAFE, CONSTRAINED e REJECTED. In CONSTRAINED LAR prova prima un modello più leggero compatibile; se non disponibile, può procedere con misure conservative e monitoraggio solo se la richiesta resta ammissibile. La fedeltà della richiesta del client è vincolante: nessuna alterazione silenziosa per renderla eseguibile. Budget, margini, soglie e parametri di freshness/isteresi/recupero sono configurabili per host. Le decisioni sono tracciabili e la simulazione non modifica l’hardware reale.

## ADR-004 — Retention, privacy e volume dati

### 1. Problema

SQLite deve mantenere storico sufficiente per confronti e audit senza crescere senza limite né conservare prompt o output sensibili per default. I risultati benchmark attuali possono includere artefatti generati/raw response, quindi una policy futura non deve cancellare o alterare retroattivamente i dati esistenti.

### 2. Alternative considerate

1. Conservare ogni dato indefinitamente.
2. Retention differenziata per classe e aggregazione della telemetria.
3. Cancellazione globale a scadenza unica.
4. Registrare il contenuto integrale di ogni inferenza per massimizzare il debug.

### 3. Vantaggi e svantaggi

- Conservazione totale supporta analisi ma accumula dati, volume e rischio privacy.
- Classi distinte conservano i dati ad alto valore e limitano campioni voluminosi; richiede riferimenti e job retention ben definiti.
- Scadenza unica è facile ma elimina benchmark o provenienza utili insieme ai campioni temporanei.
- Contenuto integrale facilita riproduzione, ma può esporre codice, segreti e dati utente; non serve per le metriche aggregate.

### 4. Impatto sull’architettura

Classi e retention iniziali approvate (tutte configurabili e versionate):

- **Benchmark normalizzati e provenienza:** indefinita. Conservare digest, configurazione e metadati necessari alla comparabilità.
- **Catalogo modelli e versioni:** indefinita, evitando duplicati.
- **Snapshot hardware grezzi:** 7 giorni.
- **Aggregati hardware:** 90 giorni.
- **Riepiloghi hardware giornalieri:** 2 anni.
- **Metriche inferenze:** 365 giorni, distinte dai risultati benchmark.
- **Eventi e log ordinari:** 14 giorni.
- **Prompt, risposte e codice generato:** non persistiti per default.
- **Contenuti salvati esplicitamente:** 90 giorni, configurabili, opt-in e separati dalle metriche.

Tutte le durate sono configurabili e versionate. I valori sopra sono retention iniziali approvate e potranno essere calibrati sulla crescita reale del database; non derivano da misurazioni già effettuate. La retention deve rispettare le foreign key ed effettuare in transazione le eliminazioni, registrando un audit sintetico. In caso di pressione sul disco, eliminare prima dati temporanei, campioni grezzi e log secondo policy; non cancellare automaticamente benchmark e provenienza. Nessuna migrazione o policy retention deve cancellare o alterare automaticamente i dati legacy in `results/lar.db`.

### 5. Impatto sui contratti Python

`InferenceRequest.content_policy` deve distinguere non persistere, persistenza esplicita e retention, con default privacy-first. Event sink/store accetta metriche e reason codes indipendenti dal raw content. Model/dataset benchmark include artifact references e retention class. Le API non devono restituire contenuto recuperato da storage per default.

### 6. Impatto sul database

Separare tabelle e lifecycle per benchmark, metriche normali, resource samples, audit/decisioni, log e artefatti raw. Aggiungere campi retention class/expiry dove necessari, indici su timestamp e batch pruning con FK. Le metriche inferenze non vanno nella tabella benchmark. Applicare a `results/lar.db` solo migrazione futura documentata e backup verificato; in questa fase nessuna rimozione e nessuna migrazione.

### 7. Motivazioni

La retention differenziata preserva benchmark e provenienza utili alla comparabilità, limita la crescita dei campioni e dei log e riduce la persistenza di contenuti sensibili. Le retention iniziali sono state approvate come valori di partenza e potranno essere calibrate sulla crescita reale del database.

### 8. Decisione approvata

**APPROVATA — A (Retention differenziata):** valgono le classi e le retention iniziali indicate nella sezione 4. Tutte le durate sono configurabili e versionate e potranno essere calibrate in base alla crescita effettiva del database; non sono valori ricavati da misurazioni già effettuate. Prompt, risposte e codice generato non sono persistiti per default. Il salvataggio esplicito è opt-in, separato dalle metriche e soggetto alla propria retention. Benchmark e provenienza conservano digest, configurazione e metadati di comparabilità; le metriche inferenze ordinarie restano separate dai risultati benchmark. L’applicazione rispetta le foreign key e registra un audit sintetico delle eliminazioni. Sotto pressione disco si rimuovono prima temporanei, campioni grezzi e log secondo policy, senza cancellare automaticamente benchmark/provenienza. Nessuna migrazione o policy modifica o cancella automaticamente i dati legacy in `results/lar.db`.

## ADR-005 — Confine API, binding e sicurezza locale

### 1. Problema

L’API compatibile OpenAI può esporre accesso a modelli e risorse. Un bind su tutte le interfacce senza autenticazione consentirebbe a utenti/reti locali di inviare richieste e consumare risorse. CORS non equivale ad autenticazione. La specifica corrente non chiude bind, auth, client identification e gestione segreti.

### 2. Alternative considerate

1. Loopback per default, senza auth locale iniziale, CORS disabilitato.
2. Loopback e token obbligatorio anche per singolo utente.
3. Bind LAN/remoto con token e TLS terminato da proxy o server configurato.
4. Bind globale senza auth, confidando in firewall/isolamento host.

### 3. Vantaggi e svantaggi

- Opzione 1 riduce esposizione per il caso locale OpenCode; processi sullo stesso host possono comunque accedere.
- Opzione 2 migliora controllo locale ma richiede distribuzione sicura del token a OpenCode e gestione rotazione.
- Opzione 3 supporta client remoti, ma richiede threat model, TLS, auth, rate/queue limits, protezione log e configurazione credenziali.
- Opzione 4 è inappropriata per un servizio che può eseguire inferenza e consumare hardware senza controllo.

### 4. Impatto sull’architettura

**Decisione approvata — A:** LAR si mette in ascolto su `127.0.0.1` per impostazione predefinita. Nel profilo locale single-user l’autenticazione tramite token non è obbligatoria, ma l’autenticazione locale deve poter essere configurata. Non introdurre autenticazione obbligatoria nel normale collegamento locale tra OpenCode e LAR. Qualsiasi binding non-loopback richiede autenticazione e TLS; TLS può essere terminato da un reverse proxy. CORS è disabilitato per impostazione predefinita. Il binding loopback limita l’accesso diretto dalla rete, ma non protegge da altri processi locali.

I segreti non devono essere memorizzati in SQLite, nelle configurazioni versionate o nei log. Un request ID serve alla correlazione delle richieste e non costituisce un’identità o una credenziale; non può autorizzare operazioni. L’endpoint `/v1/models` espone esclusivamente l’alias virtuale `lar-auto`. Non introdurre un sistema di gestione utenti o ruoli: non è richiesto da questa decisione.

### 5. Impatto sui contratti Python

DTO gateway includono autenticazione client come dependency/middleware e request ID generato/validato; `safe_client_metadata` ha allowlist e limiti. Errori auth non includono segreti. Config validate su host, CORS, auth, queue/timeouts. `InferenceGateway` non accede a provider o DB direttamente.

### 6. Impatto sul database

Non memorizzare token/segreti né header Authorization. Persistire versione/hash della configurazione effettiva, mai il segreto; ID client pseudonimo solo se serve per quote/audit e con retention esplicita. Nessun log di prompt/header.

### 7. Raccomandazione motivata

Adottare l’opzione 1 come default locale e l’opzione 3 soltanto come configurazione esplicita supportata con auth e TLS. Questo mantiene il primo client locale semplice e impedisce che una futura modifica del bind apra involontariamente l’API.

### 8. Decisione proposta

**APPROVATA — A:** bind `127.0.0.1` predefinito; token non obbligatorio nel profilo locale single-user e autenticazione locale configurabile; ogni binding non-loopback richiede autenticazione e TLS, terminabile da reverse proxy; CORS disabilitato per default; segreti esclusi da SQLite, configurazioni versionate e log; request ID solo per correlazione, mai identità o credenziale; `/v1/models` espone soltanto `lar-auto`. Il loopback non protegge da altri processi locali. Non introdurre auth obbligatoria nel normale collegamento OpenCode-LAR né gestione utenti/ruoli.

## ADR-006 — Timeout, cancellazione e shutdown

### 1. Problema

Shutdown con richiesta attiva o FIFO piena può lasciare socket, lease o modello in stato incoerente. Il timeout client non coincide necessariamente con la cancellazione provider; il provider può non fermarsi immediatamente. Le richieste già in streaming non possono essere ripetute silenziosamente.

### 2. Alternative considerate

1. Terminazione immediata del processo.
2. Drain senza limite.
3. Stop-submit, drain con deadline, poi cancellazione attiva e cleanup.
4. Lasciare che l’OS chiuda socket e lease.

### 3. Vantaggi e svantaggi

- Terminazione immediata è rapida ma perde cleanup e stato.
- Drain infinito può impedire il riavvio.
- Deadline con cleanup bilancia completamento e arresto; richiede gestione cancellazione e stati persistenti.
- Delegare all’OS non assicura rilascio applicativo né audit.

### 4. Impatto sull’architettura

Durante lo shutdown LAR interrompe l’accettazione di nuove richieste. Prima concede un periodo di grazia configurabile per drenare quelle in corso; alla scadenza, le richieste ancora in coda terminano con uno stato esplicito, ad esempio `cancelled` o `not_started`. Per le richieste attive LAR richiede la cancellazione al provider, chiude lo stream verso il client e completa il proprio ciclo di richiesta. La chiusura dello stream non prova l’arresto fisico dell’inferenza.

La lease è l’occupazione logica delle risorse e dello scheduler LAR: non garantisce l’arresto fisico dell’inferenza Ollama e non dà a LAR controllo globale sulle richieste esterne, coerentemente con ADR-001. Il rilascio della lease avviene tramite cleanup garantito (`finally`), anche in caso di errore, cancellazione o timeout. La cancellazione fisica di Ollama è best-effort: LAR non la dichiara completata senza conferma effettiva del provider. Se la conferma manca, l’incertezza resta nella telemetria e la risorsa non viene presentata come sicuramente libera dal punto di vista del provider. Non si presume che Ollama sia sempre fisicamente interrompibile.

Stati ed eventi distinguono almeno: cancellazione richiesta; richiesta LAR terminata; terminazione fisica del provider confermata; terminazione fisica non confermata o sconosciuta. Devono inoltre distinguere le richieste cancellate da quelle completate normalmente. Nessun fallback automatico dopo una cancellazione esplicita o una scadenza del timeout. Errori di cleanup non mascherano l’errore originale. Timeout, grace period e comportamenti di cancellazione sono configurabili.

### 5. Impatto sui contratti Python

Mantenere `close(drain, deadline)` e `cancel()` dei contratti; rendere espliciti eventi/esiti per `cancel_requested`, richiesta LAR terminata, conferma provider, terminazione provider non confermata/sconosciuta, `cancelled`, `not_started` e completamento normale. Idempotenza della chiusura; rilascio lease exactly-once lato LAR; deadline monotonic/time-aware. Errori cleanup osservabili ma subordinati all’errore originale. Timeout, grace period e policy di cancellazione configurabili.

### 6. Impatto sul database

Eventi append-only per shutdown iniziato, drain, cancellazione richiesta, fine richiesta LAR, conferma o incertezza di terminazione provider, lease release ed errore cleanup; status terminale della richiesta e timestamp distinguono cancellazione e completamento normale. La chiusura dello stream client non è prova di terminazione fisica. Non bloccare shutdown per scrivere un log non critico. Migrazione e consistenza transazionale da definire prima dell’implementazione.

### 7. Raccomandazione motivata

Adottare l’opzione 3. È coerente con le invarianti di lease, FIFO e cleanup e non promette cancellazione fisica che Ollama non conferma. Una scadenza configurabile impedisce shutdown bloccato.

### 8. Decisione approvata

**APPROVATA — A (cancellazione cooperativa):** durante lo shutdown LAR interrompe le nuove richieste, drena per un periodo di grazia configurabile e poi termina esplicitamente quelle ancora in coda (`cancelled`/`not_started`). Per le richieste attive richiede la cancellazione al provider, chiude lo stream client e completa il ciclo LAR. La lease rappresenta l’occupazione logica delle risorse e dello scheduler LAR, non una garanzia di arresto fisico di Ollama né di controllo globale sulle richieste esterne (ADR-001). Il rilascio della lease è garantito da cleanup in `finally`, anche con errore, cancellazione o timeout; gli errori di cleanup non mascherano l’errore originale.

La cancellazione fisica Ollama è best-effort e non è dichiarata completata senza conferma effettiva del provider. Se manca conferma, LAR conserva l’incertezza nella telemetria e non presenta la risorsa come sicuramente libera dal punto di vista del provider. Chiusura dello stream e fine della richiesta LAR sono distinte dalla conferma di arresto fisico. Stati ed eventi distinguono richiesta cancellata e completata normalmente e registrano almeno cancellazione richiesta, richiesta LAR terminata, terminazione provider confermata e terminazione provider non confermata/sconosciuta. Nessun fallback automatico dopo cancellazione esplicita o timeout. Timeout, periodo di grazia e comportamenti di cancellazione sono configurabili; non si presume che Ollama possa essere sempre interrotto fisicamente.

## ADR-007 — Benchmarks, comparabilità e confidenza

### 1. Problema

Il ranking dei modelli può usare prestazioni benchmark solo quando i risultati sono esplicitamente dichiarati comparable sulla base di evidenza adeguata. Identità effettiva del modello, hardware, quantizzazione, runtime, configurazione e protocollo influenzano i risultati. I record possono avere metadati incompleti; i benchmark costituiscono una fonte separata per valutazione e ranking e non vanno confusi con le normali metriche delle inferenze operative.

### 2. Alternative considerate

1. Usare qualunque dato disponibile, normalizzato a posteriori.
2. Confrontare solo chiavi di compatibilità esplicite e mantenere gli altri dati come osservazioni non comparabili.
3. Non usare benchmark nel routing v1.
4. Soglia fissa di ripetizioni per ogni modello prima che un benchmark sia valido.

### 3. Vantaggi e svantaggi

- Opzione 1 amplia copertura ma produce ranking fuorviante e non riproducibile.
- Opzione 2 è conservativa e tracciabile, ma riduce il numero di confronti disponibili.
- Opzione 3 è sicura ma ignora l’obiettivo di usare misure reali per orientare ranking.
- Opzione 4 può migliorare robustezza, ma una soglia arbitraria non è supportata dai dati e aumenta costo di benchmark.

### 4. Impatto sull’architettura

Il ranking può utilizzare esclusivamente risultati dichiarati `comparable`. Risultati con condizioni incompatibili sono `not_comparable` ed esclusi dal confronto. Risultati con dati insufficienti, mancanti, obsoleti o non sufficientemente affidabili sono `unscored`: questo stato non equivale a prestazioni scarse e non comporta penalizzazioni nel ranking.

La valutazione esplicita di comparabilità considera almeno digest e identità effettiva del modello, quantizzazione, runtime e relativa versione, hardware fingerprint, specifica e protocollo del benchmark, configurazione e parametri di esecuzione, nonché qualità, provenienza e numero dei campioni. Il nome o la famiglia del modello da soli non stabiliscono comparabilità. Le eventuali differenze sono valutate secondo regole documentate e deterministiche; non si applicano correzioni euristiche per rendere confrontabili risultati incompatibili. Risultati ottenuti su hardware differente non sono automaticamente confrontabili mediante normalizzazione: un’eventuale normalizzazione richiede validazione e documentazione successive.

Benchmark e metriche delle inferenze operative restano fonti distinte; solo i benchmark alimentano questa valutazione e il ranking. Il ranking è deterministico e riproducibile e non introduce pesi arbitrari o coefficienti non giustificati per combinare metriche differenti. Risultati originali e provenienza sono conservati anche se non utilizzabili nel ranking. Ogni classificazione di comparabilità è tracciabile, con motivazioni e criteri applicati. Soglie minime di campionamento, criteri di confidenza e regole statistiche dettagliate saranno definiti e calibrati in Fase 1 sulla base di dati empirici.

### 5. Impatto sui contratti Python

`MetricSummary` include unità, origine, sample count, confidence, runtime e hardware; `ModelProfile` porta digest, versione e `source_run_ids`. `ModelIntelligence.compare()` applica `ComparisonPolicy` esplicita e restituisce comparabilità/motivi. `ModelRouter` non lancia benchmark né invoca LLM. Dato legacy incompleto resta referenziabile ma non comparabile per default.

### 6. Impatto sul database

Benchmark spec separata da run/measurement e da `inference_requests`/`request_metrics`. Ogni run punta a hardware, artifact/digest, quantizzazione, runtime, configurazione e protocol version. Preservare tabelle/ID esistenti, aggiungere campi nullable/mapping legacy in migrazione futura; non inventare hardware/digest mancanti.

### 7. Raccomandazione motivata

Adottare l’opzione 2 e rimandare alla Fase 1 la calibrazione empirica delle soglie statistiche. Un risultato reale ma incompleto può essere conservato senza farlo apparire comparabile né penalizzarlo come prestazione scarsa. La comparabilità resta esplicita e basata su regole deterministiche documentate.

### 8. Decisione approvata

**APPROVATA — A (comparabilità rigorosa dei benchmark).** Si applicano i vincoli descritti nell’impatto architetturale: solo risultati `comparable` partecipano al ranking; risultati incompatibili sono `not_comparable`; evidenza insufficiente o non affidabile è `unscored` e non penalizza. Provenienza, risultati originali e motivazioni della classificazione sono conservati. Il ranking è deterministico e riproducibile, senza pesi arbitrari né normalizzazione non validata. Soglie minime di campionamento, criteri di confidenza e regole statistiche dettagliate saranno definiti e calibrati in Fase 1 su dati empirici.

## ADR-008 — Configurazione operativa, snapshot e migrazioni

### 1. Problema

Non esiste configurazione operativa corrente in `config/`. Policy, TTL, riserve e limiti devono essere riproducibili e verificabili, ma segreti e valori macchina-specifici non devono essere duplicati nel DB. SQLite esistente è legacy e le modifiche ad-hoc allo schema non sono una strategia di migrazione sufficiente.

### 2. Alternative considerate

1. Solo file statico versionato nel repository.
2. File config utente con validazione; DB conserva versione/hash e snapshot non segreti dell’effettiva policy.
3. Configurazione solo SQLite.
4. Variabili ambiente non versionate come unica fonte.

### 3. Vantaggi e svantaggi

- File nel repo facilita default ma rischia di versionare dati host/segreti e non riflette override locali.
- File utente con snapshot rende esplicita l’autorità e supporta riproducibilità; serve merge/schema version.
- Solo DB rende comoda modifica dinamica, ma accoppia bootstrap e storage e può rendere difficile recupero DB.
- Ambiente è utile per deployment ma non basta a ricostruire policy se non registrato; rischio segreti nei dump.

### 4. Impatto sull’architettura

**Stato complessivo: APPROVATA — A (importazione controllata).**

**Formato approvato:** YAML è il formato principale della configurazione LAR. La configurazione utente Linux risiede sotto `~/.config/lar/`. Ogni file di configurazione dichiara una versione dello schema e la configurazione viene validata tramite Pydantic. Segreti non devono essere inseriti nei file versionati. Le modifiche incompatibili allo schema devono essere gestite esplicitamente, senza interpretazioni silenziose.

La precedenza approvata è defaults interni → file YAML → variabili d’ambiente. Valori effettivi e snapshot/hash dei soli campi non segreti in SQLite potranno essere riferiti da decisioni, benchmark e snapshot; la persistenza dovrà rispettare la strategia di migrazioni conservative approvata.

La strategia A delle migrazioni conservative è approvata. Il database legacy `results/lar.db` deve rimanere intatto e non può essere importato o modificato automaticamente all’avvio. L’importazione approvata avviene soltanto verso un nuovo database con schema LAR e deve essere avviata esplicitamente mediante una procedura dedicata. Non alterare ora `results/lar.db`, benchmark o dati.

### 5. Impatto sui contratti Python

`ConfigProvider` porta `config_version`, `effective_config_hash` e valori già validati; policy immutabili nel dominio. `LARApplication.start()` fallisce in modo leggibile su config invalida prima di aprire l’API. `Storage` esegue migrazioni esplicite e versionate; nessuna logica schema ad-hoc nel provider/benchmark executor.

### 6. Impatto sul database

Aggiungere in futuro migration registry, effective config snapshot/hash e FK alle decisioni/run. Non salvare segreti. Le migrazioni devono preservare tabelle legacy `scans`, `model_snapshots`, `benchmark_runs`, i loro ID e contenuti; tutte le conversioni in entità nuove devono restare reversibili o documentate. Nessuna migrazione in questa fase.

### 7. Raccomandazione motivata

Sono approvati YAML, la precedenza defaults → YAML → environment, le migrazioni conservative e l’importazione controllata dei dati legacy. La scelta di importazione preserva l’originale, rende esplicita la provenienza e consente di gestire dati incompleti o non convertibili senza perdita silenziosa; la procedura tecnica resta attività di Fase 1.

### 8. Decisione e stato

**Formato di configurazione: YAML — APPROVATO.** YAML è il formato principale; la configurazione utente Linux risiede sotto `~/.config/lar/`; ogni file dichiara la versione dello schema; Pydantic valida la configurazione; i segreti non vanno inseriti nei file versionati; modifiche incompatibili allo schema richiedono gestione esplicita, senza interpretazioni silenziose.

**Precedence della configurazione — A — APPROVATA.** Ordine dal meno al più prioritario: defaults interni → file YAML → variabili d’ambiente. Una variabile d’ambiente non impostata non sovrascrive il valore YAML; una variabile presente ma non valida produce un errore esplicito. Non sono ammessi fallback silenziosi quando la configurazione è esplicitamente errata. La risoluzione della configurazione effettiva deve essere deterministica e documentata. Lo snapshot/hash della configurazione effettiva previsto dall’ADR non deve contenere segreti.

**Strategia delle migrazioni — A (migrazioni conservative) — APPROVATA.** Le migrazioni sono numerate, ordinate e versionate. Prima di ciascuna migrazione si crea un backup preventivo e se ne verifica la validità prima di iniziare. Le migrazioni si eseguono in transazione quando supportato dalle funzionalità e dai vincoli SQLite. Un registro persistente tiene identificativo/versione e informazioni sufficienti a ricostruire lo stato dello schema. Dopo ogni migrazione si verifica l’integrità. In caso di errore si effettua il rollback della transazione quando possibile e si conserva il backup preventivo; non si dichiara garantito il rollback automatico per operazioni SQLite che non supportano le garanzie transazionali richieste. Ogni migrazione è idempotente oppure impedisce esplicitamente la doppia applicazione. La migrazione non è completata finché registrazione e verifiche previste non sono terminate correttamente. **Importazione dei dati legacy — A (importazione controllata) — APPROVATA.** Il database legacy `results/lar.db` deve rimanere intatto. Non sono ammesse importazioni o modifiche automatiche all’avvio. L’importazione è avviata esplicitamente tramite una procedura dedicata e scrive verso un nuovo database con schema LAR. Si conservano gli identificativi originali quando possibile; quando devono cambiare, si mantiene una mappatura esplicita. Ogni record importato conserva informazioni di provenienza, inclusa l’identità della fonte e, quando disponibile, l’identificativo originale. Le informazioni mancanti, non ricostruibili o non verificabili sono rappresentate esplicitamente come `legacy_unknown` o mediante uno stato equivalente documentato. I dati incompatibili con il nuovo schema non sono eliminati silenziosamente: sono conservati e segnalati attraverso una gestione esplicita. L’importazione deve poter essere verificata e ripetuta senza duplicare dati o corrompere quelli già importati, produrre un riepilogo dei record importati, esclusi, non convertibili e degli eventuali errori, e consentire il confronto fra database originale e risultato. Nessuna procedura presume che tutti i dati legacy siano convertibili.

**ADR-008 nel complesso: APPROVATA — A (importazione controllata).** Sono approvati: (1) formato YAML, per configurazione versionata e validata senza interpretazioni incompatibili silenziose; (2) precedence defaults → YAML → environment, per risoluzione deterministica con override ambientali espliciti; (3) migrazioni conservative, per preservare i dati e registrare/verificare le trasformazioni nei limiti supportati da SQLite; (4) importazione controllata, per mantenere intatto il database legacy e rendere tracciabili provenienza, conversioni e record non convertibili. I criteri concreti per verificare i backup, i controlli d’integrità post-migrazione, la struttura dettagliata del registro migrazioni e la procedura tecnica di importazione/verifica dei dati legacy restano attività di Fase 1 e non bloccano l’approvazione. Non applicare migrazioni o importazioni ora.

## ADR-009 — Modello virtuale e discovery esposta

### 1. Problema

Ollama scopre più modelli fisici, mentre il client OpenAI chiede un nome. Esportare il catalogo fisico potrebbe permettere al client di aggirare routing e admission, contraddicendo il modello virtuale stabilito.

### 2. Alternative considerate

1. Esporre solo `lar-auto` nell’API OpenAI; mantenere modelli fisici interni.
2. Esporre alias virtuale e ogni modello fisico.
3. Richiedere il nome fisico al client e saltare router.

### 3. Vantaggi e svantaggi

- Opzione 1 mantiene LAR owner del routing, riduce accoppiamento client-modello; limita selezione manuale.
- Opzione 2 aiuta debugging ma introduce bypass e comportamenti diversi dal routing automatico.
- Opzione 3 semplifica l’inferenza diretta ma contraddice requisito `lar-auto` e independence del router.

### 4. Impatto sull’architettura

L’unico modello esposto ai client è l’alias virtuale `lar-auto`. `GET /v1/models` restituisce esclusivamente `lar-auto` e le richieste di inferenza devono usare tale alias. I client non possono selezionare direttamente i modelli fisici Ollama tramite l’API pubblica. L’alias resta indipendente dal modello fisico effettivamente selezionato.

La discovery dei modelli fisici è responsabilità interna di LAR; Ollama è il provider della prima versione. Il catalogo interno conserva le informazioni necessarie alla valutazione dei modelli, incluse disponibilità, caratteristiche, benchmark e compatibilità. Il router seleziona deterministicamente il modello fisico più adatto sulla base delle policy e delle informazioni disponibili. Discovery e catalogo interno non dipendono dall’elenco restituito dall’API pubblica. I modelli fisici restano identificabili e gestibili internamente, anche se non esposti ai client.

In v1 non si aggiunge alcun endpoint amministrativo per esporre il catalogo fisico. Eventuali API amministrative future richiederanno una decisione architetturale separata. Non si introduce un secondo router né un secondo agente LLM per scegliere i modelli.

### 5. Impatto sui contratti Python

`InferenceRequest.model_alias` accetta esclusivamente `lar-auto`; `InferenceGateway.list_models()` restituisce solo tale alias e non chiama direttamente tags per esporre modelli fisici. La discovery provider alimenta il catalogo e la valutazione interna di `ModelIntelligence`/router, indipendentemente dall’elenco pubblico. La scelta fisica e le sue motivazioni restano identificabili internamente.

### 6. Impatto sul database

Nessuna nuova tabella richiesta; catalogo fisico e alias/API vanno distinti se la relazione viene persistita. Routing decision conserva l’alias richiesto e il modello fisico selezionato.

### 7. Raccomandazione motivata

Adottare l’opzione 1, già coerente con i vincoli approvati: solo `lar-auto` nell’API OpenAI della prima versione.

### 8. Decisione approvata

**APPROVATA — A (alias unico).** L’unico modello esposto ai client è `lar-auto`; `/v1/models` restituisce esclusivamente questo alias e tutte le richieste di inferenza devono utilizzarlo. I client non selezionano modelli fisici Ollama via API pubblica e l’alias è indipendente dal modello fisico scelto. LAR gestisce internamente la discovery (Ollama in v1), il catalogo dei modelli e la selezione deterministica del router sulla base di policy e informazioni disponibili. Il catalogo include disponibilità, caratteristiche, benchmark e compatibilità; discovery e catalogo non dipendono dall’elenco pubblico e i modelli fisici restano identificabili e gestibili internamente. In v1 non si aggiungono endpoint amministrativi per il catalogo fisico; eventuali API amministrative future richiedono un’ADR separata. Non si introduce un secondo router né un secondo agente LLM per la scelta dei modelli.

## ADR-010 — Integrazione OpenCode e coda durevole degli eventi

### 1. Problema

L’integrazione deve osservare operazioni OpenCode e registrare in LAR eventi correlabili senza assumere che gli hook, gli stream o il recovery forniscano una cronologia completa. La consegna HTTP può interrompersi, il processo OpenCode può terminare e il plugin non deve rallentare o dirigere il workflow dell’applicazione osservata. Occorre distinguere le osservazioni ricevute dai fatti verificati e conservare una rappresentazione esplicita di lacune, incertezza e perdita.

### 2. Alternative considerate

1. Solo hook del plugin: copertura puntuale e invio diretto, ma nessuno stream esterno per osservazione continuativa o recovery.
2. Solo listener esterno: accesso allo stream e recovery best effort, ma minore copertura di hook puntuali.
3. Architettura ibrida con plugin OpenCode v2 e listener esterno, con ricezione, normalizzazione, correlazione e persistenza in LAR.

È approvata l’opzione 3. Le alternative non soddisfano insieme acquisizione puntuale, stream/recovery e limiti di autorità di LAR.

### 3. Decisione: confine di responsabilità e acquisizione

**Stato: APPROVATA.** L’integrazione usa un plugin OpenCode v2 per hook puntuali e invio HTTP, affiancato da un listener esterno per lo stream eventi e il recovery best effort. LAR riceve, normalizza, correla e persiste osservazioni. LAR non dirige i workflow e non sostituisce OpenCode. OpenCode mantiene la responsabilità di agenti, strumenti, terminale, filesystem e workflow. LAR registra solo operazioni osservabili tramite questa integrazione o direttamente tramite le proprie richieste.

Gli eventi del listener non sono automaticamente completi né affidabili per la sola provenienza. Lo stream è volatile; il recovery è best effort. Eventi mancanti non sono ricostruiti per supposizione. Acquisizione plugin, stream listener e recovery conservano la provenienza e restano distinguibili.

Il registro LAR è append-only durante il periodo di retention: le osservazioni persistite non vengono modificate né sovrascritte. La loro eliminazione è consentita esclusivamente secondo le regole di retention di ADR-004. Ogni osservazione ricevuta da plugin, listener o recovery è conservata separatamente; le correlazioni logiche risiedono in rappresentazioni separate e non modificano né sovrascrivono le osservazioni originali. La deduplicazione automatica è ammessa solo con identità nativa affidabile. Un fingerprint cross-source individua esclusivamente candidati da verificare e non autorizza fusione automatica. Osservazioni incerte o non associabili restano separate/non associate finché non vi siano prove sufficienti.

La deduplicazione non deve confondere ciclo di vita dell’evento ed esito dell’operazione. `completed` descrive il ciclo dell’evento e non implica successo dell’operazione: codice di uscita ed esito sono rappresentati separatamente.

### 4. Decisione: identità, workflow e correlazione

LAR genera il proprio `workflow_id`. Ogni nuovo messaggio utente apre un workflow; una singola sessione OpenCode può contenerne più di uno. Non è stata verificata una relazione nativa che colleghi sempre `inboxID` a `assistantMessageID`, e non si dichiara dimostrata una correlazione nativa uno-a-uno fra messaggio utente e risposta assistant. Gli eventi `session.execution.*` descrivono un periodo busy della sessione e possono coprire più drain: non sono attribuiti automaticamente a un singolo workflow né trattati da soli come esito terminale di esso.

`workflow_status`, `session_execution_observation` e `correlation_status` restano dimensioni separate. Non si usano prossimità temporale, ordine o sequenza come prova causale: correlare è consentito solo tramite identificativi nativi o catene dimostrabili. Quando la correlazione o l’esito non sono dimostrabili, si rappresentano `incomplete` o `unknown` secondo il campo pertinente. L’incertezza non viene trasformata in successo o fallimento. Eventi non associabili restano non associati e la cronologia non è dichiarata completa quando esistono lacune.

### 5. Decisione: privacy e dati ammessi

Persistire esclusivamente campi allowlistati, minimizzati e necessari allo scopo dichiarato: identificativi nativi e ID LAR strettamente necessari, categoria dello strumento, stati/esiti, timestamp/durate, modello e metriche token disponibili. Il payload integrale degli eventi non è persistito per default. Non ammettere percorsi, metadata liberi o campi arbitrari solo perché presenti nell’evento.

Sono esclusi prompt, risposte e completion, reasoning, contenuti dei tool, comandi, environment, percorsi, diff, snapshot e metadata liberi, oltre a contenuto dei file, segreti e configurazioni private. I dati fuori allowlist o che non superano i controlli di privacy sono scartati e non accodati. Questa regola integra ADR-004 e prevale sugli esempi più permissivi di metadati sopra riportati.

Le osservazioni raw/log ordinarie dell’integrazione seguono la retention di 14 giorni di ADR-004; correlazioni e dati temporanei hanno la stessa retention degli eventi cui si riferiscono. I Markdown di progetto restano documentazione e non sono soggetti a questa cancellazione. La coda di trasporto del plugin non è archivio storico: la retention degli eventi LAR non prolunga la conservazione nella coda e l’eliminazione dalla coda dopo conferma LAR non equivale alla cancellazione della corrispondente osservazione storica LAR.

### 6. Decisione: coda locale durevole del plugin

La decisione progettuale è una coda durevole locale (SQLite dedicato, indipendente dai database LAR e OpenCode, percorso `~/.local/state/lar-opencode/queue.db`, permessi restrittivi, WAL e `synchronous=FULL`). Il comportamento SQLite nel plugin, inclusi persistenza e recovery dopo riavvio, non è però verificato: il test runtime è ancora inconclusivo. Non presentare queste garanzie progettate come funzionamento dimostrato.

La coda è un trasporto durevole, non un archivio storico. Un evento viene rimosso solo dopo conferma durevole di LAR; stati `accepted` e `duplicate` sono rimovibili. Eventi in attesa e in quarantena restano conservati. Contatori diagnostici aggregati possono essere mantenuti senza conservare inutilmente contenuti già consegnati.

Il plugin applica normalizzazione e controlli di privacy; persiste localmente ciascun evento prima di considerarlo acquisito. La scrittura locale è breve e limitata nel tempo. Se fallisce o supera il timeout, il plugin segnala esplicitamente la possibile perdita. L’invio HTTP avviene in background tramite worker indipendente: nessuna richiesta HTTP si esegue sincronicamente dentro gli hook, e il plugin non attende LAR per permettere a OpenCode di proseguire. Problemi della coda non devono bloccare OpenCode.

La coda ha dimensione massima configurabile, priorità degli eventi e spazio riservato agli eventi importanti. Preserva lifecycle, ingressi, consegne, esiti ed errori. In overflow aggrega o elimina prima gli eventi a basso valore, come i delta, e registra esplicitamente ogni aggregazione o perdita; la perdita selettiva deve essere osservabile e distinguibile dalla normale assenza di eventi. La quarantena è esclusa dall’eliminazione automatica. Se un evento importante non può essere salvato, la perdita è segnalata esplicitamente. La lentezza o indisponibilità di LAR non deve bloccare né rallentare intenzionalmente OpenCode; retry e persistenza durevole degli eventi acquisiti restano richiesti. Nessun evento viene rimosso dalla coda prima della conferma di persistenza LAR.

### 7. Decisione: ordine, conferme e idempotenza

La consegna assegna priorità tra sessioni diverse e mantiene l’ordine di sequenza nativa degli eventi nella stessa sessione. L’ordinamento tramite `durable.seq` non dimostra causalità tra messaggi, workflow ed execution. Un evento non viene consegnato quando manca un predecessore necessario alla sua interpretazione. Se un precedente finisce in quarantena, il worker segnala esplicitamente il buco nella sequenza; può consegnare gli eventi successivi, marca la cronologia come incompleta e non inventa correlazioni causali.

LAR conferma un evento solo dopo validazione e persistenza durevole nel proprio database. Il protocollo prevede le risposte semantiche `accepted` (evento persistito), `duplicate` (già persistito), `rejected` (non valido o incompatibile) e `retryable_error` (problema temporaneo). La garanzia è consegna at-least-once con deduplicazione idempotente, non exactly-once. Identificativi stabili e vincoli di unicità rendono idempotenti i reinvii; se la risposta HTTP si perde dopo la persistenza, un nuovo invio non crea duplicati.

### 8. Decisione: retry, quarantena e credenziali

Per HTTP 401 il plugin rilegge il token dal file e riprova una sola volta. Se il secondo tentativo fallisce, sospende la consegna, segnala l’errore e conserva gli eventi in coda. Per errori temporanei, timeout, connessioni interrotte e HTTP 5xx applica backoff esponenziale con jitter, attesa massima configurabile e nessun limite complessivo ai tentativi.

Gli errori permanenti spostano gli eventi in una quarantena distinta dagli eventi in attesa. Si conservano motivo del rifiuto e metadati diagnostici; non si eliminano automaticamente. Ispezione, correzione, reinvio o eliminazione richiedono azione esplicita.

LAR ascolta esclusivamente su `127.0.0.1` per questa integrazione. Usa un token locale dedicato, generato da LAR con generatore crittograficamente sicuro, nel file `~/.config/lar/integrations/opencode.token`. Il file ha permessi `0600` e la directory è protetta. Il plugin legge il token da quel file. Il token non è registrato in database, log o Markdown. La rotazione avviene solo esplicitamente tramite comando LAR, invalida immediatamente il token precedente e non è periodica; dopo un 401 il plugin rilegge il file.

Questa decisione introduce una deroga specifica e motivata alla regola generale di ADR-005 che non richiede auth obbligatoria nel normale collegamento locale OpenCode-LAR: l’endpoint dedicato usato dal plugin OpenCode richiede il token di integrazione. Il default generale resta invariato per gli altri accessi locali single-user; il listener OpenCode resta vincolato a loopback. La deroga limita l’accesso al canale osservativo e separa la sua credenziale dalle credenziali generali LAR.

### 9. Decisione: spazio, corruzione e recovery

Dimensione massima, riserva per eventi importanti e gestione per priorità sono configurabili. La quarantena non è soggetta a eliminazione automatica; la gestione dello spazio non blocca OpenCode e non perde eventi senza segnalazione.

All’avvio viene verificata l’integrità SQLite. In caso di corruzione si conserva sempre il database originale e i relativi file WAL/SHM durante il recovery. Il recupero si tenta su un nuovo database, senza sovrascrivere l’originale; il risultato è verificato nuovamente per integrità, schema e identificativi. Non si ricrea mai silenziosamente una coda vuota.

È approvato il recupero parziale automatico degli eventi ancora leggibili, previa verifica della loro integrità e identificativi. Il plugin registra un avviso diagnostico di perdita potenziale, marca come incompleta la cronologia interessata, non inventa quantità né contenuti persi e conserva l’originale per ulteriori tentativi. Se non si ottiene un database recuperato verificabile, la consegna viene sospesa e si richiede un intervento esplicito.

Per il recovery dal server OpenCode v2.2.7 si usa `GET /api/experimental/session/:sessionID/log`, con cursore `after` esclusivo. `follow=true` prosegue con gli eventi live; `log.synced` è un marker di sincronizzazione, non un evento terminale. Il cursore avanza soltanto dopo il commit dell’evento in LAR; gli inserimenti sono idempotenti usando identificativi nativi. Il feed globale `/api/event` non è una fonte affidabile di recovery.

### 10. Vincoli progettuali e aspetti da verificare

Le seguenti sono decisioni progettuali delegate, non affermazioni su comportamento già implementato. Le successive scelte tecniche devono rispettare tutte le decisioni sopra e questi vincoli:

1. Preferire soluzioni semplici, verificabili e incrementali.
2. Non introdurre dipendenze nuove se quelle presenti sono sufficienti.
3. Usare identificativi stabili e vincoli di unicità per l’idempotenza.
4. Gestire transazioni, crash e riavvii.
5. Separare acquisizione, persistenza, consegna e correlazione.
6. Rendere configurabili limiti, timeout, backoff, soglie e dimensioni.
7. Non rappresentare valori mancanti come zero.
8. Non dichiarare completa una cronologia con lacune o incertezze.
9. Non cancellare dati senza regola esplicita e verificabile.
10. Rendere ogni perdita, recovery parziale e incertezza rappresentabile e diagnosticabile.
11. Non compromettere reattività o stabilità di OpenCode.
12. Non fare di LAR un agente che dirige o ricostruisce autonomamente il workflow.

Decisioni approvate: correlazione rigorosa e stati separati; priorità e perdita osservabile in overflow; recovery session log con cursore commit-aware; allowlist privacy-first. Limitazioni verificate: nessuna relazione nativa sempre valida inboxID/assistantMessageID è stata verificata; gli eventi terminali `session.execution.*` possono coprire più drain; il feed globale `/api/event` non è recovery affidabile; il test runtime SQLite del plugin resta inconclusivo. Prove ancora aperte: capacità runtime effettive di OpenCode v2.2.7 e implementazione delle API/hook, schema e campi esatti degli eventi e identificativi nativi disponibili, persistenza/commit durevole lato LAR, funzionamento SQLite/WAL/SHM e recovery in crash, e semantica concreta degli stati di incompletezza. Restano inoltre da definire soglie e limiti numerici, strumenti di quarantena e dettagli operativi. Non dichiarare verificata l’implementazione né la correlazione uno-a-uno non dimostrata.

## Nota storica sulle correzioni documentali

La precedente lista di correzioni proposte descriveva il lavoro documentale pianificato in una revisione precedente. È stata superata dal consolidamento documentale del 21 novembre 2026: il baseline Git è aggiornato nella gap analysis e nella roadmap, le fasi completate e ancora aperte sono distinte, e le evidenze disponibili sono riportate senza presentare i componenti progettati come implementati. Questa nota non modifica le decisioni ADR né approva implementazioni o migrazioni.

## Questioni che restano da approvare o calibrare

### Decisioni ancora da approvare

Nessuna. ADR-001–ADR-010 risultano approvate.

### Scelte che richiedono evidenza o definizione in Fase 1

- Valori numerici host-specific per budget e margini RAM/VRAM/CPU/disco, freshness, soglie termiche warning/trip/recovery, isteresi e condizioni di recupero.
- Limiti dimensionali dei log/database e calibrazione futura delle retention iniziali sulla crescita effettiva.
- Adapter tokenizer supportati, parametri e valori numerici di calibrazione della stima e soglia per ranking affidabile.
- Deployment per accesso remoto.
- Capacità effettive del server Ollama supportato, cancellazione, campi `/api/ps` e visibilità metriche sulla macchina target.
- Criteri concreti per verificare i backup.
- Controlli d’integrità post-migrazione.
- Struttura dettagliata del registro migrazioni.
- Procedura tecnica di importazione e verifica dei dati legacy.
- Mapping fisico e piano backup/recovery del DB legacy, nel rispetto dell’importazione controllata approvata; non modificare `results/lar.db`.
- Soglie minime di campionamento, criteri di confidenza e regole statistiche dettagliate per i benchmark, da definire e calibrare in Fase 1 sulla base di dati empirici.
- Per ADR-010: prove runtime ancora aperte su SQLite nel plugin, API/hook OpenCode v2.2.7 e persistenza durevole LAR, oltre a parametri numerici e procedura operativa di recovery. Le limitazioni di correlazione e le regole approvate sono esplicitate nella sezione 10; non si afferma una correlazione nativa uno-a-uno né implementazione verificata.
