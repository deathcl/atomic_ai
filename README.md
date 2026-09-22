# Atomic Decomposition Proxy

Proxy HTTP compatible con la API de OpenAI (`/v1/chat/completions`) que se coloca delante de un modelo LLM "upstream" (por defecto, DeepSeek) y, en vez de reenviar la conversación tal cual, **descompone cada instrucción en un árbol de subtareas atómicas, las resuelve una por una y luego sintetiza una respuesta final**.

La idea: modelos más pequeños o más baratos suelen fallar en tareas compuestas porque intentan resolverlo todo de un tirón. Este proxy fuerza un proceso de tres fases —planificar, ejecutar, sintetizar— para que cada paso sea lo bastante simple como para resolverse bien, manteniendo compatibilidad total con clientes que ya hablan el protocolo de OpenAI (incluye streaming SSE, `tool_calls`, contenido multimodal y `reasoning_content`).

## Video

<p align="center">
<b>1. Teoría</b> — cómo funciona el proceso de descomposición atómica, sin mostrar aún el script:<br><br>
<a href="https://www.youtube.com/watch?v=ruscNB4dLL4"><img src="https://img.youtube.com/vi/ruscNB4dLL4/hqdefault.jpg" alt="Teoría de la descomposición atómica"></a>
</p>

<p align="center">
<b>2. Demo</b> — el script en acción, probado en vivo:<br><br>
<a href="https://www.youtube.com/watch?v=OdK6iUHGamo"><img src="https://img.youtube.com/vi/OdK6iUHGamo/hqdefault.jpg" alt="Demo del script funcionando"></a>
</p>

## Cómo funciona

Cada turno del usuario pasa por tres fases, orquestadas por `AtomicDecompositionEngine` ([app/engine.py](app/engine.py)):

1. **Descomposición** — el modelo decide si la instrucción es atómica (resoluble en un solo paso) o si conviene dividirla en subtareas concretas y ordenadas. Se aplica recursivamente hasta una profundidad máxima configurable, construyendo un árbol de tareas.
2. **Ejecución de hojas atómicas** — cada tarea atómica del árbol se resuelve en su propia llamada al modelo, con el resultado de las tareas anteriores como contexto acumulado. Si el modelo necesita usar una herramienta (`tool_calls`), la ejecución se pausa y se le devuelve el `tool_calls` al cliente, tal como espera el protocolo de OpenAI.
3. **Síntesis final** — con todos los resultados atómicos ya resueltos, se genera la respuesta final que efectivamente se entrega al usuario (el resto del proceso se transmite como `reasoning_content`, no como la respuesta visible).

El detalle de cada fase (criterios de atomicidad, cómo se le explica al modelo que existen herramientas sin dárselas como ejecutables, reglas de seguridad ante prompt injection) vive en los prompts de [app/prompts/](app/prompts).

### Sesiones y pausa/reanudación

Como una tarea atómica o la síntesis pueden requerir `tool_calls`, el proxy necesita "recordar" en qué punto del árbol se quedó entre una petición HTTP y la siguiente (el cliente responde con el resultado de la herramienta en una request nueva). `SessionStore` ([app/session.py](app/session.py)) guarda ese estado **en memoria (default) o en SQLite** (`SESSION_BACKEND=sqlite`, sobrevive a reinicios del proceso), indexado por un hash encadenado del historial de mensajes, para poder:

- Reanudar exactamente donde quedó pausado, sin rehacer descomposición ni tareas ya resueltas.
- Detectar cuándo una request es un turno nuevo sobre una conversación ya completada (y sembrarlo con el resumen de turnos previos, en vez de redecomponer todo el historial crudo desde cero).
- Expirar sesiones por TTL y limitar cuántas se mantienen en memoria.

### Compatibilidad con el protocolo OpenAI

- Acepta `stream: true/false`, `tools`, `tool_choice`, contenido multimodal (texto + imágenes) y responde en el mismo formato (`chat.completion` / `chat.completion.chunk` vía SSE).
- El razonamiento interno del proxy (qué subtareas identificó, en qué va) se expone opcionalmente como `reasoning_content`, configurable con `TRACE_MODE` (o `EXPOSE_REASONING_CONTENT`, que tiene prioridad si está definida).
- El `system` prompt real del caller nunca se descarta: se antepone como capa de autoridad sobre los prompts internos de cada fase.

### Robustez y observabilidad

- **Presupuesto global de ejecución**: límites de subtareas por nodo, tareas totales, llamadas al upstream, segundos totales y caracteres de contexto acumulado (`app/budget.py`). Al alcanzarlos, el motor degrada de forma controlada en vez de infinitar.
- **Reintentos**: el cliente upstream reintenta errores transitorios (429/5xx, timeouts, red) con backoff exponencial + jitter y respeta `Retry-After` (`app/upstream.py`).
- **Métricas**: cada request puede exponer métricas agregadas — llamadas y reintentos por fase, tokens por modelo y coste estimado en USD si configuras `MODEL_PRICES` (`app/observability.py`), activables con `EXPOSE_METRICS`.

### Modelos por fase, perfiles y verificación opcional

- **Modelos/proveedores por fase**: descomposición, ejecución, síntesis y verificación pueden apuntar a modelos, `base_url` y API keys distintos (`PLANNER_MODEL`, `EXECUTOR_BASE_URL`, `SYNTHESIS_API_KEY`, …); si se dejan vacíos se usa el upstream por defecto.
- **Perfiles** (`ATOMIC_PROFILE`): `fast` (fast path para tareas ya atómicas), `balanced` (default) y `quality` (modelo premium opcional vía `QUALITY_MODEL`, con verificación de resultado).
- **Verificación final y paralelismo** (opcionales, `ENABLE_VERIFICATION` / `ENABLE_PARALLEL_TASKS`): una fase extra revisa el resultado con hasta `VERIFICATION_MAX_REVISIONS` revisiones, y las subtareas sin dependencias entre sí pueden ejecutarse en paralelo (hasta `MAX_PARALLEL_TASKS`).
- **Imágenes inteligentes** (`MULTIMODAL_FORWARDING`): `always` (default), `smart` (solo en la fase que mencione contenido visual) o `execution_only` (solo en la ejecución atómica).

## Estructura del proyecto

```
app/
  main.py            Endpoints FastAPI, parseo de requests, streaming SSE
  engine.py          Motor de las 3 fases (descomposición, ejecución, síntesis)
  decomposition.py   Reglas de atomicidad, límites y parseo de la descomposición
  runtime.py         Configuración de ejecución por fase (modelos, límites, trazas)
  profiles.py        Perfiles de ejecución (fast / balanced / quality)
  params.py          Parámetros de generación por fase (temperatura, max_tokens)
  budget.py          Presupuesto global (tareas, llamadas, tiempo, caracteres)
  context.py         Construcción y compresión del contexto entre fases
  tools.py           Utilidades de tools / tool_choice por fase
  upstream.py        Cliente HTTP hacia el upstream con reintentos y métricas
  observability.py   Métricas de la ejecución (tokens por fase, coste estimado)
  session.py         Sesiones en memoria o SQLite (pausa/reanudación)
  content.py         Utilidades para separar/recomponer contenido multimodal
  schemas.py         Modelos Pydantic del request/response (formato OpenAI)
  sse.py             Helpers para construir chunks de streaming SSE
  config.py          Configuración vía variables de entorno (.env)
  prompts/           Prompts de cada fase, en Markdown
tests/               Suite de pytest (unitarios + end-to-end con upstream fake)
run.py               Arranca el servidor con uvicorn
```

## Requisitos

- Python 3.9+
- Un endpoint upstream compatible con la API de chat completions de OpenAI (por defecto, DeepSeek)

## Descarga

Desde una terminal, ubicado en la ruta donde quieras tener el proyecto:

```bash
git clone https://github.com/Nichonauta/atomic_ai.git
cd atomic_ai
```

## Instalación y ejecución

Crea un entorno virtual e instala las dependencias:

```bash
python -m venv .venv
.venv\Scripts\activate      # Windows
# source .venv/bin/activate   # Linux / macOS
pip install -r requirements.txt
```

Copia `.env.example` a `.env` y completa tus valores (variables detalladas en [Configuración](#configuración)):

```bash
cp .env.example .env
```

Arranca el servidor:

```bash
python run.py
```

En Windows también puedes usar `run.bat`, que activa el entorno virtual y arranca el servidor.

El proxy queda disponible en `http://127.0.0.1:8000` (o el host/puerto configurado), exponiendo:

- `POST /v1/chat/completions` — endpoint principal, compatible con clientes OpenAI
- `GET /v1/models` — lista el modelo configurado
- `GET /healthz` — healthcheck

Apunta cualquier cliente compatible con la API de OpenAI (SDK oficial, agentes de código, etc.) a esta URL como `base_url`.

## Configuración

Variables de entorno disponibles en `.env` (todas opcionales; los valores por defecto son los de la tabla):

### Base

| Variable | Descripción | Default |
|---|---|---|
| `UPSTREAM_BASE_URL` | URL base del modelo upstream | `https://api.deepseek.com` |
| `UPSTREAM_API_KEY` | API key del upstream (también `DEEPSEEK_API_KEY`) | *(vacío)* |
| `UPSTREAM_MODEL` | Modelo a usar si el request no especifica uno | `deepseek-v4-flash` |
| `MAX_DECOMPOSITION_DEPTH` | Profundidad máxima del árbol de subtareas | `3` |
| `MAX_TOOL_ROUNDS_PER_PHASE` | Límite de rondas de `tool_calls` por fase | `25` |
| `PROXY_HOST` / `PROXY_PORT` | Dirección donde escucha el proxy | `127.0.0.1:8000` |
| `REQUEST_TIMEOUT_SECONDS` | Timeout de cada llamada al upstream | `120` |
| `LOG_LEVEL` | Nivel de log estructurado | `INFO` |

### Presupuesto global de ejecución

| Variable | Descripción | Default |
|---|---|---|
| `MAX_SUBTASKS_PER_NODE` | Máximo de subtareas por nodo | `6` |
| `MAX_TOTAL_TASKS` | Máximo de tareas en el árbol | `25` |
| `MAX_UPSTREAM_CALLS` | Máximo de llamadas al upstream por request | `40` |
| `MAX_EXECUTION_SECONDS` | Duración máxima del run completo | `300` |
| `MAX_INPUT_CHARS` | Máximo de caracteres de entrada | `200000` |
| `MAX_ACCUMULATED_CONTEXT_CHARS` | Máximo de contexto acumulado entre fases | `300000` |
| `MAX_PREVIOUS_RESULTS_CHARS` | Máximo de resultados previos reutilizados | `50000` |

### Reintentos del upstream

| Variable | Descripción | Default |
|---|---|---|
| `UPSTREAM_MAX_RETRIES` | Reintentos por llamada ante errores transitorios | `3` |
| `UPSTREAM_RETRY_BASE_SECONDS` | Base del backoff exponencial | `1` |
| `UPSTREAM_RETRY_MAX_SECONDS` | Tope del backoff exponencial | `10` |
| `UPSTREAM_MAX_RETRY_AFTER_SECONDS` | Tope al respetar `Retry-After` | `30` |

### Modelos y proveedores por fase

Vacíos = se usa `UPSTREAM_MODEL` / `UPSTREAM_BASE_URL` / `UPSTREAM_API_KEY`.

| Variable | Descripción | Default |
|---|---|---|
| `PLANNER_MODEL` / `EXECUTOR_MODEL` / `SYNTHESIS_MODEL` / `VERIFICATION_MODEL` | Modelo por fase | *(vacío)* |
| `PLANNER_BASE_URL` / `EXECUTOR_BASE_URL` / `SYNTHESIS_BASE_URL` / `VERIFICATION_BASE_URL` | Proveedor por fase | *(vacío)* |
| `PLANNER_API_KEY` / `EXECUTOR_API_KEY` / `SYNTHESIS_API_KEY` / `VERIFICATION_API_KEY` | API key por fase | *(vacío)* |
| `PLANNER_TEMPERATURE` | Temperatura de la descomposición | `0.0` |
| `EXECUTOR_TEMPERATURE` | Temperatura de la ejecución atómica | `0.2` |
| `INTERNAL_MAX_TOKENS` | `max_tokens` de las llamadas internas (si el upstream lo soporta) | *(sin límite)* |

### Fast path, perfiles y fases opcionales

| Variable | Descripción | Default |
|---|---|---|
| `ATOMIC_FAST_PATH` | Si la descomposición marca atómica, ejecutar directo | `false` |
| `ALWAYS_SYNTHESIZE` | Forzar síntesis aunque haya una sola tarea | `false` |
| `ATOMIC_PROFILE` | Perfil: `fast` \| `balanced` \| `quality` | `balanced` |
| `QUALITY_MODEL` | Modelo premium para el perfil `quality` | *(vacío)* |
| `ENABLE_VERIFICATION` | Fase extra de verificación del resultado | `false` |
| `VERIFICATION_MAX_REVISIONS` | Revisiones máximas de la verificación | `1` |
| `ENABLE_PARALLEL_TASKS` | Ejecutar en paralelo subtareas sin dependencias | `false` |
| `MAX_PARALLEL_TASKS` | Máximo de subtareas en paralelo | `4` |

### Sesiones

| Variable | Descripción | Default |
|---|---|---|
| `SESSION_TTL_SECONDS` | Tiempo de vida de una sesión pausada | `1800` |
| `MAX_SESSIONS` | Máximo de sesiones | `200` |
| `SESSION_BACKEND` | Backend: `memory` \| `sqlite` | `memory` |
| `SESSION_DATABASE_PATH` | Ruta de la base SQLite | `./data/sessions.db` |

### Multimodal y observabilidad

| Variable | Descripción | Default |
|---|---|---|
| `MULTIMODAL_FORWARDING` | Reenvío de imágenes: `always` \| `smart` \| `execution_only` \| `off` | `always` |
| `TRACE_MODE` | Razonamiento interno: `off` \| `summary` \| `full` | `summary` |
| `EXPOSE_REASONING_CONTENT` | Compatibilidad histórica; tiene prioridad sobre `TRACE_MODE` si está definida | *(sin definir)* |
| `EXPOSE_METRICS` | Exponer métricas agregadas de la request | `true` |
| `MODEL_PRICES` | Precios por millón de tokens (JSON) para estimar coste en USD | `{}` |

## Documentación ampliada

- **[`docs/GUIA_DEL_PROXY.md`](docs/GUIA_DEL_PROXY.md)** — guía completa: cómo funciona una petición, todos los parámetros con sus valores por defecto, endpoints, streaming, headers y ejemplos prácticos (curl, Python SDK, function calling, imágenes, `.env` de ejemplo).
- **[`docs/MEJORAS_IMPLEMENTADAS.md`](docs/MEJORAS_IMPLEMENTADAS.md)** — detalle técnico de cada mejora nueva: presupuesto, reintentos, modelos por fase, perfiles, verificación, paralelismo, observabilidad, persistencia de sesiones, dónde vive cada una y cómo verificarla.

## Tests

```bash
pytest
```

La suite cubre el motor de descomposición, el manejo de sesiones, el contenido multimodal, los schemas y un flujo end-to-end contra un upstream simulado (`tests/test_fake_upstream.py`).
