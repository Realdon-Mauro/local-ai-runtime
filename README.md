# Local AI Runtime

LAR è un runtime locale che espone un endpoint chat OpenAI-compatible e instrada
le richieste verso Ollama. Nella v1 è esposto un solo modello virtuale: `lar-auto`.
Il gateway, lo Scheduler FIFO, il Model Manager e il provider Ollama sono Python
in un unico processo; SQLite conserva lo schema runtime e gli strumenti di
benchmark esistenti.

## Prerequisiti

- Python 3.12 o successivo.
- Ollama installato e raggiungibile (default `http://127.0.0.1:11434`).
- Almeno un modello chat installato in Ollama. LAR non scarica modelli in modo
  automatico.

## Installazione e primo avvio

Dal checkout del repository:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
ollama pull qwen2.5:7b
lar config init
lar config validate
lar db migrate
lar status
lar serve
```

`lar config init` crea `~/.config/lar/config.yaml` con permessi `0600` e non
sovrascrive file esistenti. `--config /percorso/file.yaml` seleziona un altro
file; le variabili `LAR_*` prevalgono sul YAML. `lar config show` oscura il token.
`lar status` restituisce stato provider, integrità SQLite e fonti risorsa; valori
non disponibili sono lasciati tali, non convertiti in zero.

La configurazione predefinita ascolta solo su loopback, disabilita CORS e non
abilita un token. Per un bind non-loopback sono obbligatori `api_token`,
`tls_cert_file` e `tls_key_file`; il server Uvicorn termina TLS localmente. Per
esempio:

```yaml
bind_host: 0.0.0.0
bind_port: 8000
api_token: "inserire-un-segreto-da-file-protetto"
tls_cert_file: /etc/lar/tls.crt
tls_key_file: /etc/lar/tls.key
```

È preferibile fornire il segreto con `LAR_API_TOKEN` anziché inserirlo nel file.
Non esporre LAR direttamente su reti non fidate.

## Richieste HTTP

```sh
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/v1/models
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"lar-auto","messages":[{"role":"user","content":"Ciao"}]}'
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"lar-auto","stream":true,"messages":[{"role":"user","content":"Ciao"}]}'
```

Il gateway implementa un subset testato di Chat Completions, incluso SSE e
passaggio di tool call; non dichiara compatibilità completa con tutte le
estensioni OpenAI. La terminazione fisica di una generazione Ollama non è
verificabile dal provider attuale. Dopo una cancellazione o un timeout LAR
mantiene quindi lo stato `provider_stop_unknown`, blocca nuove inferenze e
richiede verifica/intervento operatore. Non usa `/api/ps` come prova di stop.
Il blocco sopravvive ai riavvii e la retention non elimina il record. Dopo aver
verificato lo stop tramite osservazione operativa esterna, l’operatore registra
esplicitamente l’attestazione con `lar safety confirm-provider-stopped
--request-id ID --i-verified-provider-stopped`; il comando non arresta né verifica
Ollama e il runtime va poi riavviato.

## OpenCode

È possibile configurare un provider OpenAI-compatible diretto a `lar-auto` in
`opencode.json` (schema corrente della configurazione provider OpenCode):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "lar/lar-auto",
  "providers": {
    "lar": {
      "name": "Local AI Runtime",
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": { "baseURL": "http://127.0.0.1:8000/v1" },
      "models": { "lar-auto": { "name": "LAR Auto" } }
    }
  }
}
```

OpenCode richiede una configurazione provider custom per endpoint OpenAI-compatible
non presenti nel catalogo; LAR espone il solo ID `lar-auto`. L’integrazione
osservativa prevista da ADR-010 è parziale: il gateway riceve osservazioni
minimizzate autenticate e genera un workflow ID per `user_message_received`, ma
plugin, listener, recovery e outbox durevole lato client non sono inclusi. LAR non
ricostruisce sessioni né attribuisce eventi senza identificativi dimostrabili.

Per predisporre la credenziale locale dell’endpoint osservativo:

```sh
lar integration opencode-token init
```

Il token non viene stampato; un plugin dovrebbe leggerlo da
`~/.config/lar/integrations/opencode.token`. L’endpoint è disponibile solo con
bind loopback e accetta esclusivamente campi allowlistati. Non inviare prompt,
risposte o payload arbitrari.

## Diagnostica e database

```sh
lar --config ~/.config/lar/config.yaml config show
lar db check
lar db backup
lar db prune
lar db import-legacy-benchmarks --source ./results/lar.db
```

Il database operativo predefinito è `~/.local/state/lar/lar.sqlite3`.
`results/lar.db` è il database legacy dei benchmark e viene rifiutato come target
runtime; non viene mai sovrascritto o importato automaticamente all’avvio. Il
comando esplicito `lar db import-legacy-benchmarks` apre la sorgente in sola lettura,
crea un backup verificato del target e importa misure/provenienza disponibile.
La retention ADR-004 gira
all’avvio; `lar db prune` la esegue anche su richiesta e registra un riepilogo
audit. I backup sono verificati prima di essere restituiti. `lar serve` si arresta con SIGINT/SIGTERM; lo shutdown
chiude l’ammissione e applica il drain configurato prima della cancellazione.

## Stato e limiti della v1

La suite standard usa fake provider e mock HTTP, non richiede un server Ollama.
Il routing e l’admission deterministici sono implementati. Decisioni, candidati,
profilo hardware e snapshot pre-routing vengono persistiti senza prompt; i
profili benchmark non sono ancora collegati allo storage del router, quindi il
ranking operativo resta `unscored`. I valori
hardware mancanti rimangono `unavailable` e non sono trattati come misure reali.
Al momento sono disponibili adapter host CPU/RAM/storage e NVIDIA via
`nvidia-smi`; il supporto ad altre GPU e la telemetria di temperature dipendono
dall’hardware e dal sistema operativo.

L’API `/health/live`, `/health` e `/ready` è pubblica per diagnostica locale;
non sono esposti endpoint amministrativi. Non c’è fallback automatico dopo
cancellazione, timeout o output consegnato al client.

Un fallback limitato prova un’alternativa già ammessa solo quando il caricamento
del modello primario riceve HTTP 404 prima dell’inferenza. Errori di rete o load
ambigui non fanno partire un altro modello; la decisione resta tracciata.

Per benchmark legacy usare `lar-bench --help`. Il runner legacy non è ancora
collegato al database benchmark normalizzato del gateway; l’import è esplicito.
