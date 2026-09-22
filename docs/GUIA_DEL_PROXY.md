# Guía completa del proxy

Todo lo que hace **Atomic Decomposition Proxy**: cómo funciona, todas sus capacidades y todos sus parámetros, con ejemplos claros de cada uno. El detalle técnico de las mejoras módulo a módulo está en [MEJORAS_IMPLEMENTADAS.md](MEJORAS_IMPLEMENTADAS.md).

## 1. Qué es

Proxy HTTP **compatible con la API de OpenAI** que se coloca delante de un modelo ("upstream") y, en lugar de reenviar la conversación tal cual, **descompone cada instrucción en un árbol de subtareas atómicas, las ejecuta una a una y sintetiza la respuesta final**.

El cliente no nota el proceso: mismos endpoints, mismo formato de respuesta, streaming SSE, `tool_calls`, imágenes y `reasoning_content`. La idea es ayudar a modelos pequeños o baratos que fallan en tareas compuestas: con este proceso cada paso es lo bastante simple para resolverse bien.

## 2. Cómo funciona una petición

```
Cliente ──POST /v1/chat/completions──▶ Proxy ──▶ Upstream (modelo real)
                │
                ├─ Fase 1 · Descomposición  · planificador: ¿atómico o subtareas?
                │                            · recursivo hasta MAX_DECOMPOSITION_DEPTH
                │                            · JSON validado (1 reintento si falla)
                ├─ Fase 2 · Ejecución    · cada hoja atómica en su propia llamada,
                │                            con el trabajo previo como contexto
                │                            · paralela si ENABLE_PARALLEL_TASKS y no hay
                │                              herramientas con efectos secundarios
                │                            · aquí pueden surgir tool_calls → PAUSA
                └─ Fase 3 · Síntesis       · respuesta final visible para el usuario
                                             · verificada si ENABLE_VERIFICATION=true
```

Puntos clave del flujo:

1. **El `system` del cliente no se descarta**: se conserva como `<instrucciones_del_cliente>` y se anteponen los prompts internos de cada fase como complemento operativo, no como autoridad superior.
2. **Solo se descompone la instrucción del turno actual**; el historial de turnos previos viaja como `<historial_conversacion>` (fondo, sin redecomponer).
3. **La Fase 1** pide JSON (`response_format: json_object`) y recibe las herramientas del cliente **como texto** (para clasificar atomicidad), nunca como tools ejecutables.
4. **La Fase 2** sí recibe las `tools` reales: si el modelo invoca una, el run **se pausa**, el proxy guarda la sesión y devuelve `tool_calls` al cliente con `finish_reason: "tool_calls"`, igual que haría OpenAI.
5. **La reanudación** ocurre en la petición siguiente (cliente que devuelve los resultados de las herramientas): el proxy retoma exactamente donde quedó, sin redescomponer ni repetir trabajo ya hecho.
6. **Solo la Fase 3** produce el contenido visible; las fases 1 y 2 se narran como `reasoning_content` (según `TRACE_MODE`).

## 3. Pausa y reanudación con `tool_calls` (ejemplo de 2 requests)

**Request 1** — el ejecutor decide leer un archivo:

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" -d '{
  "model": "deepseek-v4-flash",
  "messages": [{"role": "user", "content": "¿Qué puerto usa config.yaml?"}],
  "tools": [{"type": "function", "function": {
    "name": "read_file", "description": "Lee un archivo de texto",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                   "required": ["path"]}}}]
}'
```

La respuesta incluye `finish_reason: "tool_calls"` y el mensaje `assistant` con la llamada. El proxy guardó la sesión (árbol, resultados parciales, fase pendiente).

**Request 2** — el cliente ejecuta la herramienta y responde con el estándar OpenAI (`assistant` con `tool_calls` + mensaje `tool`):

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" -d '{
  "model": "deepseek-v4-flash",
  "messages": [
    {"role": "user", "content": "¿Qué puerto usa config.yaml?"},
    {"role": "assistant", "tool_calls": [{"id": "call_1", "type": "function",
      "function": {"name": "read_file", "arguments": "{\"path\":\"config.yaml\"}"}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "port: 8000"}
  ],
  "tools": [{"type": "function", "function": {"name": "read_file", "description": "Lee un archivo de texto",
    "parameters": {"type": "object"}}}]
}'
```

El proxy reconoce la reanudación por el hash encadenado del historial, continúa esa hoja con la salida de la herramienta en la conversación y termina el run (síntesis incluida). Varias rondas de herramientas dentro de la misma hoja repiten este ciclo; si se supera `MAX_TOOL_ROUNDS_PER_PHASE`, se fuerza texto para no loopear.

## 4. Endpoints

| Método | Ruta | Qué hace |
|--------|------|----------|
| `POST` | `/v1/chat/completions` | endpoint principal (streaming y no streaming) |
| `GET` | `/v1/models` | devuelve el modelo upstream configurado como lista |
| `GET` | `/healthz` | `{"status": "ok"}` para comprobar que vive |

```bash
curl http://127.0.0.1:8000/healthz        # {"status":"ok"}
curl http://127.0.0.1:8000/v1/models      # lista con UPSTREAM_MODEL
```

## 5. Arranque

```bash
cd atomic_ai
python -m venv .venv                       # una sola vez
.venv\Scripts\activate                     # Windows (source .venv/bin/activate en Linux/macOS)
pip install -r requirements.txt
copy .env.example .env                     # y ajusta UPSTREAM_* con tu modelo real
python run.py                              # o run.bat en Windows
```

Escucha por defecto en `http://127.0.0.1:8000` (`PROXY_HOST` / `PROXY_PORT`). Toda la configuración va en `.env`; todas las variables son opcionales.

## 6. Todos los parámetros

Defaults reales de `app/config.py`; `.env.example` trae comentarios por bloques.

### 6.1 Upstream (modelo real detrás del proxy)

| Variable | Default | Descripción |
|----------|---------|-------------|
| `UPSTREAM_BASE_URL` | `https://api.deepseek.com` | URL base del proveedor |
| `UPSTREAM_API_KEY` | `""` | clave (si está vacía se lee `DEEPSEEK_API_KEY` del entorno) |
| `UPSTREAM_MODEL` | `deepseek-v4-flash` | modelo por defecto y el que lista `/v1/models` |
| `REQUEST_TIMEOUT_SECONDS` | `120` | timeout HTTP por llamada |
| `UPSTREAM_MAX_RETRIES` | `3` | intentos totales por llamada |
| `UPSTREAM_RETRY_BASE_SECONDS` | `1` | base del backoff exponencial |
| `UPSTREAM_RETRY_MAX_SECONDS` | `10` | techo del delay entre intentos |
| `UPSTREAM_MAX_RETRY_AFTER_SECONDS` | `30` | techo aplicado a `Retry-After` |

### 6.2 Proxy

| Variable | Default | Descripción |
|----------|---------|-------------|
| `PROXY_HOST` | `127.0.0.1` | interfaz de escucha |
| `PROXY_PORT` | `8000` | puerto |
| `LOG_LEVEL` | `INFO` | nivel de los logs estructurados |

### 6.3 Descomposición y rondas de herramientas

| Variable | Default | Descripción |
|----------|---------|-------------|
| `MAX_DECOMPOSITION_DEPTH` | `3` | profundidad máxima del árbol de subtareas |
| `MAX_TOOL_ROUNDS_PER_PHASE` | `25` | rondas máximas de `tool_calls` por fase; al superarlas se fuerza texto |

### 6.4 Presupuesto global de ejecución

| Variable | Default | Descripción |
|----------|---------|-------------|
| `MAX_SUBTASKS_PER_NODE` | `6` | subtareas máximas por nodo |
| `MAX_TOTAL_TASKS` | `25` | tareas máximas en todo el árbol |
| `MAX_UPSTREAM_CALLS` | `40` | llamadas máximas al upstream (reintentos incluidos) |
| `MAX_EXECUTION_SECONDS` | `300` | tiempo máximo total del run |
| `MAX_INPUT_CHARS` | `200000` | caracteres máximos de la instrucción a descomponer |
| `MAX_ACCUMULATED_CONTEXT_CHARS` | `300000` | tope del contexto acumulado en ejecución |
| `MAX_PREVIOUS_RESULTS_CHARS` | `50000` | resultados previos reenviados a cada hoja |

### 6.5 Modelos y proveedores por fase

Cada fase puede usar modelo/URL/clave distintos; lo vacío cae al modelo de la request (`UPSTREAM_MODEL` como último fallback).

| Variable | Default | Fase |
|----------|---------|------|
| `PLANNER_MODEL` / `PLANNER_BASE_URL` / `PLANNER_API_KEY` | `""` | Fase 1 · descomposición |
| `EXECUTOR_MODEL` / `EXECUTOR_BASE_URL` / `EXECUTOR_API_KEY` | `""` | Fase 2 · hojas atómicas |
| `SYNTHESIS_MODEL` / `SYNTHESIS_BASE_URL` / `SYNTHESIS_API_KEY` | `""` | Fase 3 · síntesis |
| `VERIFICATION_MODEL` / `VERIFICATION_BASE_URL` / `VERIFICATION_API_KEY` | `""` | verificación (si `ENABLE_VERIFICATION=true`) |

```env
PLANNER_MODEL=deepseek-v4-flash        # barato para clasificar
EXECUTOR_MODEL=deepseek-v4-flash
SYNTHESIS_MODEL=gpt-4o-mini            # mejor redacción en la respuesta final
SYNTHESIS_BASE_URL=https://api.openai.com/v1
SYNTHESIS_API_KEY=sk-...
```

### 6.6 Temperaturas y tokens internos

| Variable | Default | Descripción |
|----------|---------|-------------|
| `PLANNER_TEMPERATURE` | `0.0` | temperatura de la Fase 1 (clasificación estable) |
| `EXECUTOR_TEMPERATURE` | `0.2` | temperatura de la Fase 2 |
| `INTERNAL_MAX_TOKENS` | *(vacío)* | `max_tokens` de las fases internas si se define |

Los parámetros del cliente (`temperature`, `max_tokens`, `top_p`…) se aplican **solo** a la respuesta visible (síntesis o fast path); la Fase 1 y 2 usan estos valores internos. El `seed` sí se propaga para poder repetir runs.

### 6.7 Fast path, síntesis forzada y perfiles

| Variable | Default | Descripción |
|----------|---------|-------------|
| `ATOMIC_FAST_PATH` | `false` | si todo es una sola hoja atómica, entregarla directo (sin síntesis) |
| `ALWAYS_SYNTHESIZE` | `false` | forzar la Fase 3 aunque haya una sola hoja |
| `ATOMIC_PROFILE` | `balanced` | perfil por defecto: `fast`, `balanced` o `quality` |
| `QUALITY_MODEL` | *(vacío)* | modelo extra que usa el perfil `quality` si está definido |

El perfil se puede forzar por petición con el header `X-Atomic-Profile` (ver ejemplo en §8). Efectos:

| Perfil | Límites | Flags |
|--------|---------|-------|
| `fast` | prof. 1 · 3 subtareas/nodo · 6 tareas · 8 llamadas · 120 s | fast path, `TRACE_MODE=summary`, sin verificación |
| `balanced` | los de §6.3/§6.4 | sin cambios (config base) |
| `quality` | prof. 4 · 8 subtareas/nodo · 40 tareas · 60 llamadas · 600 s | verificación activa, `TRACE_MODE=full`, `QUALITY_MODEL` si existe |

### 6.8 Verificación final y paralelismo

| Variable | Default | Descripción |
|----------|---------|-------------|
| `ENABLE_VERIFICATION` | `false` | tras la síntesis, una fase verifica la respuesta y la corrige |
| `VERIFICATION_MAX_REVISIONS` | `1` | correcciones completas máximas antes de entregar |
| `ENABLE_PARALLEL_TASKS` | `false` | ejecutar en oleadas las hojas con dependencias declaradas |
| `MAX_PARALLEL_TASKS` | `4` | tamaño máximo de cada oleada |

La verificación pide JSON (`{"ok": true|false, "revised": "..."}`); si `ok` es falso, reaplica `revised` y vuelve a verificar hasta agotar revisiones. El paralelismo solo actúa si la descomposición declaró `depends_on` **y** no hay herramientas con efectos secundarios entre las `tools` del cliente; el orden se calcula por orden topológico (Kahn) y las hojas se agrupan en oleadas de a lo más `MAX_PARALLEL_TASKS`.

### 6.9 Multimodalidad

| Variable | Default | Descripción |
|----------|---------|-------------|
| `MULTIMODAL_FORWARDING` | `always` | a qué fases se reenvían las `image_url` de la request |

| Valor | Comportamiento |
|-------|----------------|
| `always` | imágenes en descomposición, ejecución y síntesis |
| `execution_only` | solo en la ejecución atómica |
| `smart` | solo si el texto menciona contenido visual (imagen, foto, captura, screenshot, diagrama, gráfico…) |
| `off` | nunca; solo texto |

```env
MULTIMODAL_FORWARDING=smart
```
Con `smart`, pedir "describe esta captura" adjuntando la imagen hace que solo las fases relevantes la reciban: menos tokens que `always` y más precisión que `execution_only`.

### 6.10 Observabilidad y trazas

| Variable | Default | Descripción |
|----------|---------|-------------|
| `EXPOSE_METRICS` | `true` | headers `X-Atomic-*` en las respuestas |
| `MODEL_PRICES` | `{}` | precios por millón de tokens para estimar coste (clave = modelo) |
| `TRACE_MODE` | `summary` | `off` (sin trazas), `summary` (progreso de fases) o `full` (+ detalle interno) |
| `EXPOSE_REASONING_CONTENT` | *(sin definir)* | compatibilidad histórica: si se define, manda sobre `TRACE_MODE` (`true`→`full`, `false`→`off`) |
| `LOG_LEVEL` | `INFO` | nivel de los logs estructurados (p. ej. `upstream_retry`, `profile_unknown`) |

```env
MODEL_PRICES={"deepseek-v4-flash":{"input":0.27,"output":1.10}}
```
Con precios configurados, las respuestas no-streaming terminadas incluyen el header `X-Atomic-Cost-Usd` con el coste estimado del run.

### 6.11 Sesiones (pausa/reanudación y turnos)

| Variable | Default | Descripción |
|----------|---------|-------------|
| `SESSION_BACKEND` | `memory` | `memory` (proceso) o `sqlite` (sobrevive a reinicios) |
| `SESSION_DATABASE_PATH` | `./data/sessions.db` | ruta del SQLite cuando el backend es `sqlite` |
| `SESSION_TTL_SECONDS` | `1800` | vida de una sesión sin usarse |
| `MAX_SESSIONS` | `200` | sesiones en memoria máximas (al llenarse se expiran las más antiguas) |

Las sesiones guardan el árbol, los resultados parciales y la fase pendiente para poder responder `tool_calls` y retomar en la siguiente petición; se emparejan con el historial mediante hash encadenado.

**Precedencia de la configuración:** por petición (headers `X-Atomic-Profile`/`X-Atomic-Trace` y parámetros de la request) > variables de entorno/`.env` > defaults de `app/config.py`.

## 7. Headers de respuesta (observabilidad)

Con `EXPOSE_METRICS=true` (default) cada respuesta lleva:

```bash
curl -si http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"hola"}]}' | Select-String X-Atomic

X-Atomic-Request-Id: 3f9c1e2a-...
X-Atomic-Profile: balanced
X-Atomic-Duration-Ms: 4210          # solo en respuestas terminadas (no streaming)
X-Atomic-Cost-Usd: 0.0042           # solo si MODEL_PRICES cubre los modelos usados
```

## 8. Ejemplos por petición

### 8.1 Forzar un perfil con un header

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "X-Atomic-Profile: fast" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"Resume esto en una frase: ..."}]}'
```

Esa petición usa los límites del perfil `fast` (profundidad 1, fast path…) aunque el `.env` diga otra cosa. La respuesta confirma el perfil en `X-Atomic-Profile: fast`.

### 8.2 Enviar una imagen

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" -d '{
  "model": "deepseek-v4-flash",
  "messages": [{
    "role": "user",
    "content": [
      {"type": "text", "text": "¿Qué se ve en esta captura?"},
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0..."}}
    ]
  }]
}'
```

Con `MULTIMODAL_FORWARDING=smart` (§6.9), la imagen se adjunta solo a las fases que hablan de contenido visual.

### 8.3 Ver el proceso interno (`reasoning_content`)

Con `TRACE_MODE=summary` (default) o `full` en el `.env`, la respuesta incluye trazas del proceso:

```json
{
  "choices": [{
    "message": {
      "content": "El puerto configurado es 8000.",
      "reasoning_content": "Fase 1 de 3. Primero comienzo dividiendo la tarea...\n- Leer config.yaml (atómica)\n..."
    },
    "finish_reason": "stop"
  }]
}
```

`TRACE_MODE=off` elimina `reasoning_content`; `full` añade detalle interno (trabajo por tarea, decisiones de presupuesto).

## 9. Streaming (SSE)

Con `"stream": true` el proxy emite el estándar OpenAI: chunks `data:` con `delta`, terminando en `data: [DONE]`.

```bash
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-flash","stream":true,"messages":[{"role":"user","content":"haz una lista"}]}'
```

```
data: {"id":"chatcmpl-...","choices":[{"delta":{"role":"assistant"}}]}
data: {"id":"chatcmpl-...","choices":[{"delta":{"reasoning_content":"Fase 1 de 3..."}}]}
data: {"id":"chatcmpl-...","choices":[{"delta":{"content":"1. "}}]}
data: {"id":"chatcmpl-...","choices":[{"delta":{"content":"Paso uno\n"}}]}
...
data: {"id":"chatcmpl-...","choices":[{"delta":{},"finish_reason":"stop"}]}
data: [DONE]
```

Si el ejecutor pide una herramienta, en mitad del stream llega un chunk con `delta.tool_calls` y `finish_reason: "tool_calls"` seguido de `[DONE]` (la sesión queda pausada, §3). Un error del upstream durante el stream se emite como `data: {"error": {...}}` y `[DONE]`.

## 10. Cabeceras útiles por petición

Además del payload, estas cabeceras se tienen en cuenta:

| Cabecera | Efecto |
|---|---|
| `X-Atomic-Profile: fast\|balanced\|quality` | Perfil de ejecución de este run (si `ATOMIC_PROFILE` no está ya fijado por entorno). |
| `X-Atomic-Trace: off\|summary\|full` | Trazas `reasoning_content` de este run. |
| `X-Request-Id` | Identificador propio para correlación en logs. |

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Atomic-Profile: quality" \
  -H "X-Request-Id: mi-tarea-42" \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"refactoriza el módulo X"}]}'
```

## 11. Ejemplos completos

### 11.1 Python (SDK oficial de OpenAI)

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="no-importa")

# Chat simple: el proxy decide internamente si descompone o no
resp = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=[{"role": "user", "content": "¿Qué es la fotosíntesis en una frase?"}],
)
print(resp.choices[0].message.content)

# Streaming con trazas del proceso
stream = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=[{"role": "user", "content": "Escribe una función Python que valide un email"}],
    stream=True,
)
for chunk in stream:
    delta = chunk.choices[0].delta
    if getattr(delta, "reasoning_content", None):
        print("[interno]", delta.reasoning_content, end="")
    if getattr(delta, "content", None):
        print(delta.content, end="")

# Con herramientas del cliente (function calling)
resp = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=[{"role": "user", "content": "¿Qué tiempo hace en Madrid?"}],
    tools=[{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Devuelve el clima actual de una ciudad",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }],
)
msg = resp.choices[0].message
if msg.tool_calls:                       # el ejecutor pidió una herramienta
    resultado = {"temp_c": 21, "sky": "despejado"}
    resp2 = client.chat.completions.create(
        model="deepseek-v4-flash",
        messages=[
            {"role": "user", "content": "¿Qué tiempo hace en Madrid?"},
            msg,
            {"role": "tool", "tool_call_id": msg.tool_calls[0].id, "content": str(resultado)},
        ],
        tools=[/* la misma definición */],
    )
    print(resp2.choices[0].message.content)   # respuesta final consolidada
```

### 11.2 curl: descomposición visible

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4-flash",
    "messages": [
      {"role": "system", "content": "Eres un ayudante de soporte."},
      {"role": "user", "content": "Lee el informe.csv, calcula las ventas por región y redacta un resumen ejecutivo"}
    ]
  }'
```

Salida interna (en `reasoning_content`): la Fase 1 clasifica la tarea y muestra el árbol — por ejemplo `- Leer informe.csv (atómica)`, `- Calcular ventas por región (atómica)`, `- Redactar resumen ejecutivo (atómica)` —; cada tarea se ejecuta en orden y la síntesis entrega un único `content`.

### 11.3 .env de ejemplo (todas las capacidades)

```env
# Upstream
UPSTREAM_BASE_URL=https://api.deepseek.com
DEEPSEEK_API_KEY=sk-...
UPSTREAM_MODEL=deepseek-v4-flash

# Proxy
PROXY_HOST=127.0.0.1
PROXY_PORT=8000

# Presupuesto y robustez
MAX_TOTAL_TASKS=25
MAX_UPSTREAM_CALLS=40
UPSTREAM_MAX_RETRIES=3

# Modelos por fase (misma cuenta, distinto modelo)
PLANNER_MODEL=deepseek-v4-flash
SYNTHESIS_MODEL=deepseek-chat

# Perfil por defecto y trazas
ATOMIC_PROFILE=balanced
TRACE_MODE=summary

# Funciones opcionales
ATOMIC_FAST_PATH=true
ENABLE_VERIFICATION=true
ENABLE_PARALLEL_TASKS=true
MULTIMODAL_FORWARDING=smart

# Sesiones y métricas
SESSION_BACKEND=sqlite
EXPOSE_METRICS=true
```

---

Si algo no se comporta como esperas, empieza por `/metrics` (si está activo) y por `TRACE_MODE=summary`: las trazas muestran qué fase y qué tarea se está resolviendo en cada momento.

Ver también: [`docs/MEJORAS_IMPLEMENTADAS.md`](MEJORAS_IMPLEMENTADAS.md) · [`README.md`](../README.md) · [`.env.example`](../.env.example)


