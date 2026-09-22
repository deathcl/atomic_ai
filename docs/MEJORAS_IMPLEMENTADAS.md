# Mejoras implementadas — detalle técnico

Detalle de **cada mejora** añadida sobre la versión base del proxy, módulo a módulo: qué hace, dónde vive, cómo se controla (variables de entorno) y un ejemplo mínimo. La suite completa (`python -m pytest -q`) pasa con **40 tests**.

Para el uso general del proxy (arranque, flujo completo y ejemplos prácticos) ver [GUIA_DEL_PROXY.md](GUIA_DEL_PROXY.md).

| # | Mejora | Módulo principal | Control principal |
|---|--------|------------------|-------------------|
| 1 | Presupuesto global de ejecución | `app/budget.py` | `MAX_*` |
| 2 | Reintentos con backoff | `app/upstream.py` | `UPSTREAM_RETRY_*` |
| 3 | Modelos y proveedores por fase | `app/runtime.py` + `app/upstream.py` | `<FASE>_MODEL/_BASE_URL/_API_KEY` |
| 4 | Parámetros internos vs. finales | `app/params.py` | `PLANNER_TEMPERATURE`, `EXECUTOR_TEMPERATURE`, `INTERNAL_MAX_TOKENS` |
| 5 | Validación fuerte de la descomposición | `app/decomposition.py` | automático |
| 6 | Compresión de contexto | `app/context.py` | `MAX_PREVIOUS_RESULTS_CHARS` |
| 7 | Fast path para tareas atómicas y perfiles de ejecución | `app/engine.py` + `app/profiles.py` | `ATOMIC_FAST_PATH`, `ALWAYS_SYNTHESIZE`, `ATOMIC_PROFILE`, header `X-Atomic-Profile` |
| 8 | Reenvío multimodal configurable | `app/engine.py` + `app/content.py` | `MULTIMODAL_FORWARDING` |
| 9 | Verificación final y paralelismo (con clasificación de herramientas por riesgo) | `app/engine.py` + `app/tools.py` | `ENABLE_VERIFICATION`, `VERIFICATION_MAX_REVISIONS`, `ENABLE_PARALLEL_TASKS`, `MAX_PARALLEL_TASKS` |
| 10 | Métricas, coste estimado y logs | `app/observability.py` | `EXPOSE_METRICS`, `MODEL_PRICES`, `LOG_LEVEL` |
| 11 | Trazas configurables | `app/engine.py` | `TRACE_MODE` / `EXPOSE_REASONING_CONTENT` |
| 12 | Sesiones SQLite y reanudación segura | `app/session.py` | `SESSION_BACKEND`, `SESSION_DATABASE_PATH` |
| 13 | Pruebas, CI y documentación | `tests/`, `.github/workflows/` | — |

## 1. Presupuesto global de ejecución

**Qué hace.** Un único presupuesto (`ExecutionBudget`) impone límites duros a todo el run: subtareas por nodo, tareas totales, llamadas al upstream, segundos de ejecución, caracteres de entrada y de contexto acumulado. Al alcanzar un límite, el motor **deja de expandir o de llamar y cierra con el trabajo hecho** —una degradación controlada, nunca un bucle infinito—. Los límites tocados quedan en `metrics.limits_hit` y las notas de degradación en `metrics.budget_notes`.

**Dónde vive.** `app/budget.py`; lo consume `app/upstream.py` (`register_call()` en cada llamada) y el motor, y se configura desde `RuntimeConfig`.

| Variable | Default | Significado |
|----------|---------|-------------|
| `MAX_SUBTASKS_PER_NODE` | 6 | máx. subtareas que acepta un nodo al descomponerse |
| `MAX_TOTAL_TASKS` | 25 | máx. tareas en todo el árbol |
| `MAX_UPSTREAM_CALLS` | 40 | máx. llamadas HTTP al modelo (reintentos incluidos) |
| `MAX_EXECUTION_SECONDS` | 300 | tiempo máximo total del run |
| `MAX_INPUT_CHARS` | 200000 | máx. caracteres de la instrucción a descomponer |
| `MAX_ACCUMULATED_CONTEXT_CHARS` | 300000 | tope del contexto acumulado en ejecución |

**Ejemplo.**
```env
MAX_TOTAL_TASKS=10
MAX_UPSTREAM_CALLS=15
MAX_EXECUTION_SECONDS=120
```
Un objetivo enorme se trunca en ~10 tareas y como mucho 15 llamadas al modelo; si se agota el presupuesto, la síntesis final se produce con los resultados parciales disponibles.

## 2. Reintentos del upstream (backoff exponencial con jitter)

**Qué hace.** Cada llamada al modelo soporta fallos transitorios sin abortar el run: reintenta con espera creciente y aleatoriedad, y respeta la cabecera `Retry-After` del servidor (con techo).

**Dónde vive.** `app/upstream.py` — `compute_delay()`, `_is_retryable()`, `_sleep()`.

**Qué se considera reintenable:**
- Errores HTTP transitorios (`408`, `429`, `5xx`).
- Fallos de red y timeouts (`httpx.TransportError`), **incluidas desconexiones a mitad de streaming**.
- Respuestas ilegibles o incompletas (`JSONDecodeError` / `ValueError`).

Los errores no reintenables (p. ej. una `400` de validación) se lanzan como `UpstreamError` al primer intento; tras agotar reintentos, el endpoint devuelve `502` con `{"error": {"type": "upstream_error"}}`.

**Cálculo de la espera:** `min(UPSTREAM_RETRY_MAX_SECONDS, UPSTREAM_RETRY_BASE_SECONDS · 2^intento)` × factor aleatorio `0.5–1.5` (jitter). Si el servidor envía `Retry-After`, se respeta como suelo, con techo `UPSTREAM_MAX_RETRY_AFTER_SECONDS`.

| Variable | Default | Significado |
|----------|---------|-------------|
| `UPSTREAM_MAX_RETRIES` | 3 | intentos totales por llamada |
| `UPSTREAM_RETRY_BASE_SECONDS` | 1 | base del backoff |
| `UPSTREAM_RETRY_MAX_SECONDS` | 10 | tope de la espera entre intentos |
| `UPSTREAM_MAX_RETRY_AFTER_SECONDS` | 30 | techo aplicado a `Retry-After` |

**Ejemplo.**
```env
UPSTREAM_MAX_RETRIES=5
UPSTREAM_RETRY_BASE_SECONDS=2
```
Esperas aproximadas: ~2 s → ~4 s → ~8 s → ~10 s → ~10 s (con jitter). Cada reintento genera un log estructurado `upstream_retry` con la razón y el delay.

## 3. Modelos y proveedores por fase

**Qué hace.** Cada fase (planificador, ejecutor, síntesis, verificación) puede usar un **modelo, un endpoint e incluso un proveedor distintos**. Si una variable de fase está vacía, se cae al valor global (`UPSTREAM_MODEL` / `UPSTREAM_BASE_URL` / `UPSTREAM_API_KEY`).

**Dónde vive.** Resolución en `app/runtime.py` (`resolve_runtime` → `RuntimeConfig` con un `PhaseConfig` por fase); enrutado en `app/upstream.py` (`provider(phase, model)` decide modelo, URL y clave según la fase).

| Variable | Default | Significado |
|----------|---------|-------------|
| `<FASE>_MODEL` | `""` → global | modelo de esa fase (`PLANNER`, `EXECUTOR`, `SYNTHESIS`, `VERIFICATION`) |
| `<FASE>_BASE_URL` | `""` → global | endpoint de esa fase |
| `<FASE>_API_KEY` | `""` → global | clave de esa fase |
| `PLANNER_TEMPERATURE` | 0.0 | temperatura del planificador (JSON de clasificación estable) |
| `EXECUTOR_TEMPERATURE` | 0.2 | temperatura del ejecutor |
| `INTERNAL_MAX_TOKENS` | sin tope | límite de tokens de las llamadas internas |

**Ejemplo.**
```env
UPSTREAM_BASE_URL=https://api.deepseek.com
UPSTREAM_API_KEY=sk-...

# Barato para planificar; proveedor local para ejecutar;
# modelo más capaz para la respuesta final:
PLANNER_MODEL=deepseek-v4-flash
EXECUTOR_MODEL=gemma-local
EXECUTOR_BASE_URL=http://127.0.0.1:8080/v1
EXECUTOR_API_KEY=local
SYNTHESIS_MODEL=deepseek-chat
```

## 4. Parámetros de generación: internos vs. finales

**Qué hace.** Separa los parámetros de muestreo del cliente de los de las fases internas. Los parámetros del cliente (`temperature`, `top_p`, `max_tokens`, `max_completion_tokens`, `stop`, `seed`, `presence_penalty`, `frequency_penalty`, `response_format`, `parallel_tool_calls`) se aplican **solo a la llamada que produce la respuesta visible** (síntesis o fast path). Las fases internas usan `PLANNER_TEMPERATURE` / `EXECUTOR_TEMPERATURE` y `INTERNAL_MAX_TOKENS`; el `seed` sí se propaga para poder repetir runs.

**Dónde vive.** `app/params.py` — `GenerationParams.from_request()`, `final_payload()`, `internal_payload()`.

**Ejemplo.** Un cliente envía `temperature: 0.9`: la Fase 1 sigue planificando a `0.0` y el ejecutor a `0.2` (clasificación estable y ejecuciones deterministas), mientras la respuesta final al usuario sí sale con `0.9`.

## 5. Validación fuerte de la descomposición

**Qué hace.** La salida de la Fase 1 se valida con modelos Pydantic **antes** de tocar el árbol: JSON legible, descripciones no vacías, subtareas dentro de `MAX_SUBTASKS_PER_NODE`, `id` únicos, dependencias que apuntan a tareas existentes y **ausencia de ciclos**. Si algo falla, el motor reintenta **una sola vez** enviando el mensaje de `DecompositionError` (que dice exactamente qué está mal); si vuelve a fallar, la tarea se trata como atómica y el incidente queda registrado.

**Dónde vive.** `app/decomposition.py` — `parse_decomposition()`, `DecompositionPlan`, `DecompositionError`.

**Formato aceptado.**
```json
{"atomic": false, "subtasks": [
  {"id": "1", "description": "Leer la configuración", "depends_on": []},
  {"id": "2", "description": "Actualizar el puerto", "depends_on": ["1"]}
]}
```
También admite el formato clásico de strings (`"subtasks": ["haz X", "haz Y"]`) y extrae el JSON aunque venga rodeado de texto o bloques de código.

**Ejemplo de efecto.** Si el modelo devuelve `"subtasks": [{"id": "a", "description": ""}]`, no se construye ningún árbol corrupto: se reintenta con la corrección y, en el peor caso, se ejecuta la tarea tal cual (atómica), sin romper el run.

## 6. Compresión de contexto

**Qué hace.** Evita inflar los prompts con el history completo: limita cuántos caracteres de resultados anteriores viajan en `<trabajo_previo>` (recortando los más antiguos primero) y pone un tope al contexto acumulado de la fase de ejecución.

**Dónde vive.** `app/context.py` (`render_results()`), con los topes que aplica `app/budget.py`.

| Variable | Default | Significado |
|----------|---------|-------------|
| `MAX_PREVIOUS_RESULTS_CHARS` | 50000 | caracteres de resultados previos reenviados a cada hoja |
| `MAX_ACCUMULATED_CONTEXT_CHARS` | 300000 | tope total del contexto acumulado en ejecución |

**Ejemplo.**
```env
MAX_PREVIOUS_RESULTS_CHARS=10000
```
En la hoja 20 solo viajan los ~10 000 caracteres más recientes de trabajo previo; lo más antiguo queda fuera para no disparar el coste del prompt.

## 7. Reenvío multimodal configurable

**Qué hace.** Las imágenes que lleguen en cualquier mensaje (`image_url`) se reenvían a las fases internas según el modo elegido:

- `always` (default): imágenes en descomposición, ejecución y síntesis.
- `execution_only`: solo en la ejecución atómica (ahorra tokens en Fase 1 y 3).
- `smart`: solo en la fase que mencione contenido visual — se comprueba la instrucción del turno y el texto de la fase contra `IMAGE_KEYWORDS` (imagen, foto, captura, screenshot, diagrama, gráfico…).
- `off`: nunca; las fases reciben texto plano.

**Dónde vive.** `app/engine.py` — `_user_content()` y `IMAGE_KEYWORDS`; `app/content.py` — `build_multimodal_content()`.

**Ejemplo.**
```env
MULTIMODAL_FORWARDING=smart
```
Request con `[{"type":"text","text":"describe este pantallazo"}, {"type":"image_url", ...}]`: la Fase 1 clasifica **sin** imagen (su prompt no menciona contenido visual), la ejecución de la hoja **sí** la recibe adjunta, y la síntesis solo la incluye si su texto menciona la imagen.

## 8. Fast path para tareas atómicas y perfiles de ejecución

**Qué hace.**

- **Fast path** (`ATOMIC_FAST_PATH=true`): si la descomposición decide que todo el objetivo es **una sola hoja atómica**, su resultado se entrega directamente como respuesta final **sin pasar por la síntesis** (menos latencia y una llamada menos). Se desactiva solo si `ALWAYS_SYNTHESIZE=true` o la verificación está activa.
- **`ALWAYS_SYNTHESIZE=true`**: fuerza la Fase 3 aunque haya una sola hoja (útil cuando la síntesis aplica estilo propio del sistema).
- **Perfiles** (`ATOMIC_PROFILE=fast|balanced|quality`): presets que sobreescriben límites y flags por petición sin tocar el `.env`. Se pueden forzar con el header **`X-Atomic-Profile`** (tiene prioridad sobre la variable). Un nombre desconocido cae a `balanced` con un warning.

| Perfil | Efecto principal |
|--------|------------------|
| `fast` | profundidad 1, 3 subtareas/nodo, 6 tareas, 8 llamadas, 120 s, fast path activo |
| `balanced` | usa la configuración base tal cual |
| `quality` | profundidad 4, 8 subtareas/nodo, 40 tareas, 60 llamadas, 600 s, verificación activa, `TRACE_MODE=full` y `QUALITY_MODEL` si está definido |

**Dónde vive.** `app/profiles.py` (`PROFILES`, `normalize_profile`), `app/runtime.py` (`resolve_runtime` aplica el perfil y lo serializa en la sesión), `app/engine.py` (`_recompute_fast_path()`).

**Ejemplo.** Una petición puntual de máxima calidad sin editar configuración:
```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "X-Atomic-Profile: quality" -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"..."}]}'
```

## 9. Verificación final opcional y planes con paralelismo

**Qué hace.**

- **Verificación** (`ENABLE_VERIFICATION=true`): después de la síntesis, una fase extra (modelo `VERIFICATION_MODEL` o el de síntesis) revisa la respuesta contra el objetivo con salida JSON (`{"ok": …, "revised": …}`). Si `ok=false`, sustituye la respuesta por `revised` y vuelve a verificar, hasta `VERIFICATION_MAX_REVISIONS` correcciones. Si la verificación es ilegible, se registra el error y se entrega la respuesta tal cual (nunca se pierde un run por esto).
- **Paralelismo** (`ENABLE_PARALLEL_TASKS=true`): si la descomposición declaró dependencias (`depends_on`), las hojas se ejecutan en orden topológico (Kahn) en oleadas de hasta `MAX_PARALLEL_TASKS` en paralelo. Antes de paralelizar se comprueba con `has_side_effect_tools()` que las `tools` del cliente no tengan efectos secundarios (escrituras, envíos…): si los tienen, se mantiene el orden secuencial.

**Dónde vive.** `app/engine.py` — `_verify_and_maybe_revise()`, `_execution_order()`, `_run_parallel_wave()`; `app/tools.py` — `has_side_effect_tools()`.

**Ejemplo.**
```env
ENABLE_VERIFICATION=true
VERIFICATION_MAX_REVISIONS=2
ENABLE_PARALLEL_TASKS=true
MAX_PARALLEL_TASKS=4
```
Una descomposición con `{"id":"2","depends_on":["1"]}` ejecuta la tarea 2 solo después de la 1; las tareas sin dependencias compartidas corren de a 4 en oleada.

## 10. Observabilidad: métricas, coste y logs

**Qué hace.** Cada request genera un `RunMetrics` con llamadas, reintentos, duración y tokens por fase/modelo. Con `EXPOSE_METRICS=true` se refleja en headers de la respuesta:

| Header | Cuándo aparece | Contenido |
|--------|----------------|-----------|
| `X-Atomic-Request-Id` | siempre | id único del run |
| `X-Atomic-Profile` | siempre | perfil efectivo (`fast`/`balanced`/`quality`) |
| `X-Atomic-Duration-Ms` | respuestas no-streaming terminadas | duración total |
| `X-Atomic-Cost-Usd` | si `MODEL_PRICES` tiene precios para los modelos usados | coste estimado en USD |

Los logs estructurados (`log_level=INFO`) registran eventos como `upstream_retry` (con fase, motivo y delay) o `profile_unknown` (perfil inválido → `balanced`). Si `EXPOSE_METRICS=false`, no se emite ningún header.

**Dónde vive.** `app/observability.py` (`RunMetrics`, `PhaseMetrics`, `Usage`, `estimate_cost`, `log`), `app/main.py` (`_metrics_headers()`).

**Ejemplo.**
```env
MODEL_PRICES={"deepseek-v4-flash":{"input":0.27,"output":1.10}}
```
```log
upstream_retry phase=executor reason="HTTP 503" delay_seconds=1.12 request_id=req_7f3a…
```

## 11. Trazas: `TRACE_MODE` y `reasoning_content`

**Qué hace.** Controla cuánto proceso interno se devuelve al cliente en `reasoning_content` (y en chunks `reasoning` del SSE):

- `off`: sin trazas; solo el contenido final.
- `summary` (default): progreso narrado de las fases ("Fase 1 de 3…", árbol de subtareas, "Verificación: …").
- `full`: además, detalle interno (`reasoning_detail` del motor): trabajo de cada tarea, decisiones de presupuesto, etc.

`EXPOSE_REASONING_CONTENT` (histórica) tiene precedencia si está definida: `true` → `full`, `false` → `off`. Los eventos `reasoning` que llegan del propio upstream (modelos con `reasoning_content` en deltas) se reenvían siempre que el modo no sea `off`.

**Dónde vive.** `app/engine.py` — `_progress_events()` (summary/full) y `_detail_events()` (solo full); `app/config.py` — `Settings.effective_trace_mode()`; `app/main.py` (filtra antes de emitir).

**Ejemplo.**
```env
TRACE_MODE=full
```
```json
{"choices":[{"message":{
  "content":"El puerto es 8000.",
  "reasoning_content":"Fase 1 de 3…\n- Leer config.yaml (atómica)\n…Verificación: la respuesta es válida."}}]}
```

## 12. Persistencia de sesiones y reanudación segura

**Qué hace.** La sesión que sostiene la pausa/reanudación por `tool_calls` y los turnos consecutivos puede vivir en memoria o en SQLite:

- `SESSION_BACKEND=memory` (default): dict en proceso, con TTL (`SESSION_TTL_SECONDS`) y LRU por `MAX_SESSIONS`.
- `SESSION_BACKEND=sqlite`: `SqliteSessionStore` en `SESSION_DATABASE_PATH`; las sesiones **sobreviven a reinicios del proxy** (útil detrás de supervisores o en despliegues con autoactualización).

La reanudación es segura: `app/main.py` valida con hash encadenado del historial (`is_valid_resume`, `extract_tool_outputs`) que la petición entrante es realmente la continuación de la fase pausada (y no un turno disfrazado), y `is_new_turn` distingue un turno nuevo (run nuevo sembrado con `turn_history`, sin redescomponer el historial crudo). El flag `paused` en el guardado decide si se conserva o se limpia el estado pendiente, de modo que una sesión completada no se reanude por accidente.

**Dónde vive.** `app/session.py` (`MemorySessionStore`, `SqliteSessionStore`, `hash_chain`, TTL/LRU), `app/main.py` (`_resolve_run`, `_persist_session`).

**Ejemplo.**
```env
SESSION_BACKEND=sqlite
SESSION_DATABASE_PATH=./data/sessions.db
```
El cliente recibe `tool_calls`, el operador reinicia el proceso y, al volver la petición de reanudación, el proxy retoma la misma hoja con sus resultados parciales intactos.

## 13. Pruebas, CI y documentación

**Qué hay.** 40 tests (`pytest` + `pytest-asyncio`) cubriendo presupuesto, reintentos, métricas/coste, perfiles, múltiples modelos, imágenes por fase, paralelismo, verificación, fast path y reanudación, con un `FakeUpstream` que registra cada payload recibido. CI en GitHub Actions (`.github/workflows/tests.yml`) que ejecuta la suite en cada push/PR sobre varias versiones de Python. La documentación vive en `README.md`, `.env.example` y `docs/`.

---

## Cómo verificar que todo funciona

```powershell
# 1. Suite completa (40 tests, no necesita API key)
python -m pytest -q

# 2. Arrancar el proxy
python run.py

# 3. Petición mínima
curl http://127.0.0.1:8000/v1/chat/completions -H "Content-Type: application/json" `
     -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"hola"}]}'
```



