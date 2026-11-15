# Proyecto_GetNet: contexto para Claude Code

Sistema multi-agente de soporte para Getnet (LangGraph + FastAPI). Reto técnico "AI Hardcore Engineer".
Dueño: Lucca. Idioma de trabajo: **español** al conversar; comentarios, nombres y mensajes de commit en inglés/español como ya hay en el repo.
Prefiere que le presentes **trade-offs** (2-3 opciones con costo/beneficio) antes de tomar decisiones de diseño que no sean obvias. En las obvias: decide y dilo en una línea.
_Este archivo se recreó el 2026-11-15 (no existía en el disco). Plan completo y su fuente de verdad: `PLAN_95.md` (ignorado por git). Rúbrica completa de 11 criterios: `GETNET_MEJORAS_Y_RUBRICA.md` (ignorado por git)._

## Entorno
- Windows, `cmd`, entorno virtual `.venv` (Python 3.13 local; la imagen usa 3.12). Usa `.venv\Scripts\python`. Rama de trabajo: `calibracion-ciega`.
- Agentes: Fireworks `gpt-oss-120b` (router, sintetizador, knowledge, support, verificador). Juez: `qwen/qwen3.8-27b` en Groq (siempre Groq).
- Tests: `pytest -q` (502 en verde al 2026-11-15; no se necesita red ni claves). El eval real SÍ gasta API (~0.08 USD por corrida de 27 casos).
- Python en terminal: usa `python -X utf8` si imprimes texto con acentos.
- Docker 29.9 y Compose v5.7 instalados; `promtool` no está en el PATH (usar `docker run --rm --entrypoint promtool prom/prometheus ...`).

## Arquitectura (lo que importa)
`summarize -> router -> (knowledge | support, en el orden del plan) -> synthesizer -> verify -> END`; `handoff -> END`.
- `app/graph.py`: `build_graph(..., verify=None)`; `next_node` decide agente / handoff / sintetizador.
- `app/agents/`: `router.py` (RoutingDecision con `language`, `wants_human`; resetea `verify_note` cada turno), `knowledge.py`, `support.py` (tools del cliente + `escalate_to_human`), `synthesizer.py` (con 1 solo agente pasa su respuesta SIN imponer idioma), `verifier.py` (nodo verify por citas), `common.py` (`run_tool_agent`, reintento "nudge" si el agente no usa tool).
- `app/prompts.py`: todos los `*_PROMPT`. Se formatean con `.format` SUPPORT_PROMPT (`user_id`, `language`, `prior_context`) y SYNTHESIZER_* (`language`, `agent_outputs`): **no uses llaves `{}` en ellos**. ROUTER_PROMPT y JUDGE_PROMPT no se formatean. Los prompts del verificador (`VERIFY_PROMPT`, `REVISE_PROMPT`, `TRANSLATE_PROMPT`) viven en `app/agents/verifier.py` a propósito, fuera del test de hashes.
- `app/state.py` (`GraphState`, incluye `verify_note`), `app/lang.py` (detector en/pt/es sin dependencias), `app/handoff.py` y `app/handoff_store.py` (cola SQLite con lease/claim/reply/reject), `app/memory.py` (checkpointer SQLite persistente, TTL), `app/observability.py` (`TurnCollector`, `TraceStore`, `NODES`), `app/service.py`, `app/main.py`, `app/auth.py` (JWT HS256, roles merchant/operator), `app/llm.py` (un proveedor por `AGENT_PROVIDER`, sin failover), `app/rag/*` (Chroma + Voyage denso), `app/tools/*`.
- Eval: `eval/run_eval.py`, `eval/judge.py`, `eval/dataset.jsonl` (campos: id, user_id, history, message, expected_agents, optional_agents, expected_tools, expect_handoff, criteria, holdout), `eval/retrieval.jsonl` + `eval/run_retrieval.py`, `eval/repeats.py`, `eval/calibration/*`. `eval/reports/` está en `.gitignore` salvo `latest.md`, `REPEATS-*.md` y `RETRIEVAL-*.md`: para versionar un JSON crudo usa `git add -f`.
- CI: `.github/workflows/ci.yml` (pytest + `docker build` + comprobación de uid no root).

### El verificador (hecho)
El modelo ligero devuelve `Verification(statements=[Statement(statement, evidence_quote)])`. El CÓDIGO comprueba la cita (`quote_in`: subcadena normalizada sin mayúsculas/acentos/puntuación, o >= 60% de sus tripletas de palabras en la evidencia; citas de < 3 palabras deben ser literales). Sin cita válida = sin respaldo -> UNA reescritura que borra esas afirmaciones. Después, si el idioma de la respuesta difiere del del cliente (ambos claros), UNA traducción. Falla abierto. Reemplaza el último `AIMessage` reutilizando su `id`. `verify_note`: `"revised:N [afirmación | ...]"`, `"translated:pt->en"` o `""`. Límite conocido: el código solo valida las citas que el modelo da; si el modelo OMITE una afirmación, no se detecta (visto en q08).

## Reglas duras
1. **No toques las etiquetas humanas** (`labels*.csv`, columnas `human_*`) ni abras `labels_template.csv` / `labels_v2_key.csv` (llaves con las notas del juez). El etiquetado ciego lo hace Lucca.
2. **No corras `python -m eval.calibration.export` sin `-o` explícito**: su default pisa `labels_template.csv`, la llave de V1.
3. **No tunees con el holdout** (casos `ho*` y los holdout nuevos que Lucca commitee; `holdout: true` en retrieval). Se usa solo para medir.
4. Cada cambio de comportamiento lleva test. `pytest -q` en verde antes de dar una fase por cerrada.
5. **Cada cambio de un `*_PROMPT` rompe `tests/test_observability.py::test_every_prompt_is_byte_for_byte...`**: actualiza `PROMPT_SHA256` con
   `python -c "import hashlib, app.prompts as p; [print(n, hashlib.sha256(getattr(p,n).encode('utf-8')).hexdigest()) for n in dir(p) if n.endswith('_PROMPT')]"`.
6. No modifiques `JUDGE_PROMPT`, el parseo del veredicto ni el scoring de `eval/judge.py` sin avisar: cualquier cambio invalida la calibración. **Excepción aprobada (2026-11-15):** solo la capa de transporte de `eval/judge.py` (reintentos con backoff exponencial ante 429 y ante JSON no parseable; errores residuales contados aparte, nunca como fail), con test. Activar modo JSON en la petición NO está aprobado: presentarlo como opción.
7. Commits los hace Lucca. No los hagas; no agregues `Co-Authored-By` en lo que le propongas. Nunca subas `.env`.
8. Dependencias aprobadas: `cryptography`, `rank_bm25`, `prometheus-client`; solo dev: `ruff`, `mypy`, `bandit`, `pip-audit`. Cualquier otra: avisar antes.
9. Reglas de trabajo: una fase a la vez, mínimo diff, sin refactors fuera de la fila de la rúbrica. Al cerrar cada fase: tabla ítem de rúbrica -> archivo/test/comando -> resultado REAL; autoevaluación honesta (nota y qué la separa del 9.5); archivos cambiados; actualizar "Pendiente". No marcar nada como hecho sin ejecutarlo; si no se puede ejecutar (Docker, red), dar el comando exacto de Windows `cmd` con la salida esperada. Timebox: si una fase pasa de 1.5x su estimación, parar y reportar. El eval real gasta API: usar `--only` y reportar el costo.

## Calibración del juez (resultados, en `eval/calibration/`)
| | acuerdo | kappa | MAE groundedness | FP (juez pass, humano fail) | FN |
|---|---|---|---|---|---|
| V1 (juez original) | 70% | 0.20 | 0.70 | 7 | 1 |
| V2 (juez nuevo, respuestas nuevas) | 74% | 0.46 | 0.52 | 3 | 4 |

Archivos: `labels2.csv` + `labels_template.csv` = V1; `labels_v2_blind.csv` + `labels_v2_key.csv` = V2; reportes `CALIBRATION_V1.md` / `CALIBRATION_V2.md`. Comando:
`python -m eval.calibration.agreement <hoja> --judge-key <llave> -o eval\calibration\CALIBRATION_Vn.md`.
Desacuerdos V2: q04, q09, q10, x01 (FN), x04, m02, m03 (FP). Hallazgos: el juez alucinó en q09 y q10; en q05/q08 el juez tenía razón (pasos que no estaban en la evidencia).
**Importante:** después de medir V2 se cambió el juez en código (idioma detectado con `app/lang.py`; veredicto desde las notas: relevance>=4, groundedness>=4, completeness>=3, idioma ok; topes por afirmación sin respaldo). V2 ya no describe exactamente al juez actual: hay que **re-medir con ~15 filas ciegas** tras la corrida final y decirlo en el reporte.

## Estado (2026-11-15)
**Hecho:** `app/lang.py` + tests; verificador por citas + 15 tests (`tests/test_verify.py`); `SUPPORT_PROMPT` reglas 3 y 7 (rechazo de datos de otro cliente sin `escalate_to_human`; x04 pasa); reglas de router y `SYNTHESIZER_DIRECT_PROMPT` para recuerdo/reformulación (m01-m03, m06-m08); `verify_note` en estado y filas del eval.
**Último eval parcial** (`--only q05 q08 x04`, 0.019 USD): q05 **fail** (el verificador borró 7 pasos inventados y quedó una respuesta hueca con "estas acciones" colgando, completeness 1); q08 **error del juez** (JSON no parseable, no es fallo del producto; el verificador no borró nada aunque la respuesta tiene pasos sin respaldo); x04 **pass** 5/5/5 sin handoff.
**Defectos conocidos:** pasos inventados que no están en la evidencia (q05, q08); q08 tarda ~45 s; el agente de conocimiento a veces responde sin tool; el juez no espera entre reintentos (judge_errors por 429 de Groq en corridas anteriores); `conversation_id` lo elige el cliente; el eval multi-turno inyecta el historial en vez de pasar por el checkpointer.

## Pendiente (fuente: `PLAN_95.md` secciones 8-14; rúbrica completa de 11 criterios en `GETNET_MEJORAS_Y_RUBRICA.md`, ignorado por git)
**Hoy 8.73 (técnica).** Meta del candidato: **[9.5 / 9.6] SIN ELEGIR: preguntar antes de la Fase 2.** 9.5 oficial exige técnica >= 9.45; 9.6 exige >= 9.55. Con 25 h de trabajo y toda la sección 4b, mi proyección es **9.43 a ~25.5 h sin colchón** (cada ítem llegando a la nota de la rúbrica; el evaluador varía ±0.2). 9.6 no cabe (~36 h por mis cuentas; la rúbrica estima 50-60 h).
**Entrega: martes 2026-11-17 a las 9:00. La corrida final (Fase 8) debe estar LANZADA a más tardar el martes a las 5:00** (corrida ~1 h; luego Lucca etiqueta 6:00-7:30; agreement + README hasta las 8:30).
Orden aprobado: **1 -> 2 -> 3 -> 4 -> 5 -> 8 -> 6 -> 7**. Las fases que cambian comportamiento (RAG, replan, HITL) van antes de la corrida final; Observability y Production, después. Se para al final de cada fase.

### Reglas de proceso de la rúbrica (condiciones de la nota)
- Historial de commits visible: no aplanar ni squashear; un commit por fase.
- El holdout nuevo va en su propio commit ANTES de cualquier cambio de prompt. No cambio ningún `*_PROMPT` hasta que Lucca confirme ese commit.
- El holdout solo mide (`ho01-ho05` y los nuevos). Cambio de comportamiento => volver a correr eval.
- El reporte final se genera con el stack actual (Fireworks 120b + juez Groq/Qwen) y cubre TODOS los casos del dataset (hoy 30 + el holdout nuevo, incluidos los 17 originales y h01-h03), no solo "27".
- JSON crudo versionado con `git add -f eval\reports\<stamp>.json`. Reportes con su evidencia.
- Kappa sobre respuestas NUEVAS: etiquetas de filas de la corrida final; la llave del juez es de la MISMA corrida que la hoja (no re-correr entre exportar y calcular); se publica aunque sea bajo.
- Orden del juez: backoff de transporte (E1, con test) + su commit -> corrida final -> export ciego -> Lucca etiqueta -> `agreement` -> `eval/judge.py` y `JUDGE_PROMPT` CONGELADOS. Las 4 reglas de la sección 5.5 ya están en `JUDGE_PROMPT` (`app/prompts.py:175-185`).

### Ítems de la sección 4b (h = mi estimación; pts = nota oficial). Cabe = entra en 25 h sin colchón
Baratos y adyacentes al gate:
- **S4a** merchant no abre `/operator/*`: YA EXISTE (`tests/test_operator_api.py:108,131`); solo re-ejecutar. 0 h. Cabe.
- **S4b** logs de auditoría de fallos de auth sin datos sensibles (hoy solo `log.warning("auth rejected: <clase>")`): 0.5 h, +0.010. Cabe.
- **S4c** `exp` corto + denylist por `jti`: 0.75 h, +0.015. Cabe.
- **S2** `scripts/` fuera de la imagen + iss/aud distintos para operadores (+ test aud erróneo -> 401): 0.75 h, +0.030. Cabe.
- **S3** rate limit por `sub` (presentar trade-off memoria vs Redis): 0.75 h, +0.020. Cabe.
- **S5** casos de inyección REGULARES en el eval + test en CI sin red: 1.0 h, +0.015. Cabe.
- **P1a** defaults de compose y `.env.example` = Fireworks 120b: 0.25 h, +0.010. Cabe.
- **P1b** Dockerfile multi-stage (HEALTHCHECK YA existe, `Dockerfile:38`): 0.5 h, +0.010. Buffer.
- **P1c** dependencias con lock (`pip freeze` a constraints, o `pip-tools` = dependencia nueva de dev, pedir OK): 0.75 h, +0.010. No cabe.
- **T1** `create_support_ticket` idempotente + validación estricta + tests de inyección sobre tools (hoy `create_ticket` genera `uuid` cada vez): 1.0 h, +0.020. Buffer.
Caros (no caben): **M4b** Postgres ~4 h +0.010; **M4a** alternativa barata "bloqueo documentado y probado" (rechaza >1 worker) 0.75 h +0.010; **H2** webhook/Slack + SLA de handoffs ~2.5 h +0.014.
Por criterio: **R1** observe->replan 4.0 h +0.120 (opción c; a 2.0, b 3.0); **R2** medición antes/después sobre casos REGULARES + m01/m03/x04 1.5 h +0.045; **S1** RS256+JWKS+`kid`+Keycloak+tests 2.5 h +0.100; **H1** `interrupt()`+approve/reject+`Command(resume)`+vencimiento 3.0 h +0.091; **M1** m01 + failover `with_fallbacks` + test 503 sin cola humana 1.5 h +0.035; **M2** `conversation_id` del servidor 1.0 h +0.015 (no cabe); **M3** eval multi-turno con checkpointer real y reinicio 1.25 h +0.020 (no cabe); **A1** RAG BM25+RRF+rerank 3.0 h +0.070 (compite con F6); **E\*** Evaluation (backoff E1 0.75 + 5 repeticiones E2 2.0 + kappa E3 0.75) 4.0 h +0.098; **O** Observability (OTLP+Jaeger, `/metrics`, Grafana, alertas con `promtool test rules`, presupuesto de costo por turno) 4.0 h +0.114; **C1** ruff/mypy/bandit/pip-audit/trivy BLOQUEANTES 2.0 h +0.020 (no cabe).

### Fases (en orden)
- **Fase 1, cierre del verificador (~1 h).** (0) Proponer 6-8 casos de holdout nuevos (pregunta que la KB no cubre, datos de otro cliente, inyección, multi-turno, handoff, pt/en). **Esperar la confirmación de Lucca de que están commiteados** antes de tocar cualquier prompt. (1) `SUPPORT_PROMPT` regla 5: reemplazar el ejemplo "weak signal -> move closer to the router" por una conducta: si la evidencia no cubre el arreglo, decirlo en una frase y OFRECER abrir un ticket (no crearlo sin aceptación). Sin llaves `{}`. Actualizar `PROMPT_SHA256`. (2) `REVISE_PROMPT` sin referencias colgantes; si no queda nada accionable, decir que no hay pasos documentados y ofrecer ayuda, sin afirmar nada nuevo. Con test. (3) `verify_raw` (salida cruda del verificador) SOLO en las filas del eval. (4) Leer SIN modificar cómo `run_eval` trata `verdict="error"`. (5) Correr q05, q08 (x2) y x04: tabla (veredicto, groundedness, verify_note, verify_raw, unsupported_claims). Si en q08 el verificador omite afirmaciones, NO arreglarlo: presentar opciones (p. ej. partir la respuesta en frases y exigir cita por cada una).
- **Fase 2, Security + Docker/compose (~6.5 h cabe, +1.25 buffer/no cabe).** S1, S2, S3, S4b, S4c, S5, P1a; P1b y P1c si hay colchón. Keycloak en compose con realm importable; `PyJWKClient` con `kid`; RS256; tests con tokens firmados por otra clave, `exp` vencido, aud/iss erróneos y `kid` desconocido. Mantener un modo de prueba sin IdP para tests y eval. Aquí se toca `Dockerfile`/`docker-compose.yml` una sola vez.
- **Fase 3, Agentic Reasoning (~5.5 h con la opción c).** **DECISIÓN DE LUCCA PENDIENTE entre (a) extender `verify` con señales de código [2.0 h, replan tardío], (b) nodo LLM aparte tras cada agente [3.0 h, +latencia/costo, no determinista], (c) híbrido: `observe` determinista (retrieval vacío, error de tool, relevancia baja, afirmaciones sin respaldo) que elige seguir / añadir agente / reintentar con otra query / escalar, con LLM solo para reformular la query [4.0 h, medición reproducible, umbrales a calibrar]. Detalle en `PLAN_95.md` sección 13. NO decidir por él.** Máx. 2 replans, visible en routing/trace, falla abierto. Tests con LLM guionizado: sin replan, un replan, tope alcanzado, error del proveedor, y una rama por señal. Medición con interruptor `REPLAN_MAX` (0 vs 2) sobre los casos REGULARES (sin `ho*`), >= 2 repeticiones: pass rate, tasa de afirmaciones sin respaldo, llamadas LLM por turno, p50/p95, costo (~0.30 USD). Casos regulares nuevos para lo que falla siempre; nunca el holdout.
- **Fase 4, HITL + Memory (~4.5 h cabe, +4.0 buffer/no cabe).** H1, M1; T1 si hay colchón. `interrupt()` dentro de un nodo con tools re-ejecuta el nodo al reanudar: presentar opciones de diseño antes de implementar. Checkpointer SQLite ya existe. Comportamiento definido al vencer el plazo. `with_fallbacks` Fireworks->Groq con test que simula 503.
- **Fase 5, RAG (~3.0 h; compite con la Fase 6 por horas).** `rank_bm25` + RRF + rerank Voyage (`voyageai` ya instalado). Antes/después con `run_retrieval`: afinar en el conjunto regular, reportar en el holdout. Baseline: hit@1 0.27 regular / 0.33 todo, MRR 0.48. Límite de Voyage 3 req/min: usar `--pause`. Si no mejora, reportarlo.
- **Fase 8, corrida final (~4.0 h + ~1 h de reloj + 1-2 h de etiquetado de Lucca).** E1 backoff de transporte del juez (regla 6) + test + commit -> `python -m eval.run_eval --repeats 5 --pause 3 --retries 3` (0 errores 429) -> `git add -f` del JSON crudo -> reporte/README con números reales, umbral de regresión y limitaciones -> export ciego (`export -o <llave> --blind <hoja>`) -> Lucca etiqueta ~15 filas (priorizar las que usan tools) -> `agreement` -> juez congelado.
- **Fase 6, Observability (~4.0 h, después de la corrida final).** OTLP como segundo `CallbackHandler` junto a `TurnCollector`; Jaeger (o Langfuse: presentar trade-off); `/metrics` con histograma por nodo, costo, 429 y tasa de handoff; Grafana provisionado; reglas de alerta (p95, costo, 429, handoff) validadas con `promtool test rules` vía Docker, con una captura/test reproducible de que disparan.
- **Fase 7, Production (solo lo que quede).** ruff, mypy, bandit, pip-audit y trivy como pasos BLOQUEANTES del CI; imagen etiquetada; `docker compose config`.

### Cortes (horas acumuladas; excluyen tu revisión y etiquetado; nota técnica proyectada si cada ítem llega a su meta)
A = F1+F2+F3 = 13.0 h -> 9.10. B = A + F4 + F8 = 21.5 h -> 9.32. **C = B + F6 = 25.5 h -> 9.43 (sin colchón)**. C+ = C + todo lo buffer/no cabe ≈ 36 h -> 9.61 (no cabe). Con RAG en lugar de F6: ~24.5 h -> 9.39.

### Preguntas abiertas a Lucca (resolver antes de la fase indicada)
1. Meta 9.5 o 9.6 (antes de la Fase 2). 2. Opción (a), (b) o (c) para el replan (antes de la Fase 3). 3. F6 vs A1 con 25 h (antes de la Fase 5). 4. `pip freeze` vs `pip-tools` si se hace P1c. 5. Confirmar que el trabajo actual y el holdout nuevo están commiteados (el 2026-11-15 `git log` seguía en `ec1b89e`).

## Comandos útiles
- Un caso o un prefijo: `python -m eval.run_eval --only m06 q05 --pause 3 --keep-latest`
- Resultado por caso: `python -X utf8 -c "import json; d=json.load(open('eval/reports/<stamp>.json',encoding='utf-8')); [print(r['id'], r['judge']['verdict'], r['judge']['groundedness'], repr(r.get('verify_note'))) for r in d['rows']]"`
- Si un `git add` dice "ignored by .gitignore": es `eval/reports/`; usa `-f` solo para lo que sí quieras versionar.
