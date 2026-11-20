# LAR — Roadmap tecnica incrementale

Questa fase produce solo documentazione. Non modificare ~/benchmark-ai.

## Fase 0 — Baseline/decisioni (bloccante)
Verificare Git baseline: directory attuale non ha .git. Decidere esclusività Ollama, token estimation, bind/auth, resource reserves/sensors, privacy, shutdown e migration. Preparare fixture/copia DB e benchmark baseline su hardware rilevato; nessun SLO/peso prima di dati. **Uscita:** decision log e ambiente isolato, DB operativo invariato.

## Fase 1 — Contratti/config/storage
Domain immutable types, enums, exceptions, Protocol; Pydantic solo boundary; config versionata e loopback default; migration registry e repository nuovi accanto alle tabelle attuali. Test legacy, rollback, restore, counts/IDs e comparability unknown. Dipende 0. **Uscita:** schema migrabile, DB reale intatto.

## Fase 2 — Ollama chat/stream
Estendere adapter esistente per messages/tool schema/chat/SSE, event/usage/cancel/disconnect; MockTransport e live test opzionale. Conservare generate benchmark finché non serve cambiarlo. Dipende 1. **Uscita:** solo adapter conosce API Ollama.

## Fase 3 — Resource Manager
Discovery CPU/RAM/GPU; sample con source/time/freshness; external process monitoring; estimate range/confidence basati su metadata/profili reali; admission e simulation condividono policy, simulation non chiama provider. Calibrare senza garanzie. Dipende 1 e model metadata; iniziare con fixture. **Uscita:** unit/hardware tests e simulazioni verificabili.

## Fase 4 — Lifecycle/Scheduler
ModelManager single managed model, ownership/reconciliation, lease request-scoped, failure states; FIFO bounded worker, queue status, cancellation/deadline/idle/shutdown. Event/Barrier tests provano ordine/serialità; modellare client Ollama esterni. Dipende 1–3. **Uscita:** invarianti queue/model dimostrate.

## Fase 5 — Intelligence/router/fallback
Collegare artifact, benchmark, hardware/runtime; comparability su spec digest, model digest/quantizzazione e runtime; TaskAnalyzer deterministic; capability/context; Pareto/tie-break senza pesi; decision trace/alternatives/simulation; fallback solo pre-output e deadline invariata. Dipende provider stream, risorse e scheduler. **Uscita:** replay route deterministic e failure matrix.

## Fase 6 — Gateway OpenAI
FastAPI lifecycle, health/readiness, /v1/models e /v1/chat/completions; JSON/SSE/tool/error/IDs; loopback default; buffer first meaningful event prima headers. ASGI e OpenAI client contract tests. Dipende 1–5. **Uscita:** endpoint compatibile senza dipendenza OpenCode.

## Fase 7 — Observability/OpenCode/hardening
Log/event/metrics senza prompt; system tests reali systemd/bwrap/seccomp/OOM/cancel/recovery; OpenCode profile temporaneo su LAR; e2e stream/tool/cancel/FIFO/IDs/fallback pre-output/no fallback post-output; solo dopo run ripetuti definire SLO/stability/soglie; migration rehearsal su copia DB, non su file operativo. Dipende precedenti. **Uscita:** report e test effettivamente eseguiti.

Ordine rischio: Git/DB safety → provider chat/stream → manager/scheduler → resource admission → router/fallback → API → OpenCode e2e. Non riscrivere runner/provider per uniformità; mantenere lar-bench compatibile senza requisito esplicito.
