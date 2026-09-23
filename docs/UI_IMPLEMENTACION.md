# Atomic AI — Plan de implementación de la UI web

> **Objetivo de este documento.** Que cualquier persona (incluida tú en el futuro, o un colaborador) pueda abrir este archivo y saber exactamente: qué se va a construir, en qué orden, con qué reglas y cómo comprobar que cada fase funciona. Si la implementación queda a medias, basta con mirar la **checklist de reanudación** (§10) para saber por dónde seguir.

---

## 1. Visión general

Añadir una interfaz web al proxy, servida por el **mismo proceso FastAPI** que ya sirve `/v1/*`, para:

1. **Configurar** el proxy sin tocar el `.env` a mano (formulario por bloques con opciones cerradas).
2. **Ver el estado** del proxy en vivo (métricas, coste, límites, sesiones).
3. **Probar el motor** desde un playground que muestra la descomposición en tiempo real.

**Fuera de alcance (por ahora):** autenticación multiusuario, temas, plugins, edición de prompts, anything que requiera build de Node.

## 2. Principios no negociables

| # | Principio | Implicación concreta |
|---|-----------|----------------------|
| P1 | **Sin texto libre salvo obligación** | Cada parámetro del `.env` se edita con el widget que le corresponde: `select` para enums, toggles para booleanos, spinners con `min`/`max` para números. **Nunca** un `<input type="text">` genérico donde quepa lo que sea. |
| P2 | **Los campos de texto van muy restringidos** | Solo los parámetros que *exigen* escritura del usuario (URLs, modelos, API keys, rutas, JSON de precios) son de texto, y cada uno tiene patrón/longitud/formato validado **en cliente y en servidor** (validación doble, el servidor manda). |
| P3 | **El servidor valida siempre, el cliente es comodidad** | Todo lo que entre por `PUT /ui/api/config` se valida con un modelo Pydantic construido desde el catálogo. Si el cliente se bypasea, el servidor rechaza igual (`422`). |
| P4 | **Cero dependencias nuevas** | Vanilla JS + HTML + CSS servidos como estáticos. El backend solo usa FastAPI/Pydantic, ya presentes en `requirements.txt`. |
| P5 | **El `.env` es la fuente de verdad** | La UI escribe el `.env` con **backup automático** previo y validación completa antes de tocar el fichero. Nunca deja el `.env` corrupto. |
| P6 | **Seguridad por defecto** | El proxy (y por tanto la UI) escucha solo en `127.0.0.1`. Las API keys **nunca** se devuelven en claro al navegador (solo máscara). No se exponen endpoints de config sin validación. |
| P7 | **Coherente con el proyecto** | Español en la UI y en los docs; sin frameworks JS; sin paso de build. |

## 3. Arquitectura recomendada

Mismo proceso, mismos puertos, cero infraestructura nueva:

```
atomic_ai/
├── app/
│   ├── main.py                 # ya existe: /v1/*, /healthz, /v1/models → monta /ui y /ui/api
│   ├── config.py               # ya existe: Settings (BaseSettings) — fuente de verdad
│   ├── observability.py        # ya existe: RunMetrics, log()
│   ├── session.py              # ya existe: stores de sesión
│   └── web/                    # ← NUEVO paquete
│       ├── __init__.py
│       ├── catalog.py          # ← Catálogo INMUTABLE de parámetros (P1/P2)
│       ├── schemas.py          # ← Modelos Pydantic generados/derivados del catálogo (P3)
│       ├── envfile.py          # ← Lector/escritor seguro de .env con backup (P5)
│       ├── routes.py           # ← Router /ui/api/*
│       ├── registry.py         # ← Buffer en memoria de RunMetrics recientes (dashboard)
│       └── static/
│           ├── index.html      # SPA única (pestañas: Config · Dashboard · Playground)
│           ├── app.js          # vanilla JS + EventSource para streaming
│           └── style.css
├── docs/UI_IMPLEMENTACION.md   # este documento
└── tests/
    ├── test_web_catalog.py     # ← NUEVO: catálogo y validación
    ├── test_web_config.py      # ← NUEVO: PUT config (válido/inválido/backup/máscara)
    └── test_web_stats.py       # ← NUEVO: dashboard y playground
```

**Montaje en `app/main.py` (al final del módulo):**

```python
from fastapi.staticfiles import StaticFiles
from app.web.routes import router as ui_router

app.include_router(ui_router, prefix="/ui/api")
app.mount("/ui", StaticFiles(directory=str(Path(__file__).parent / "web" / "static"),
                             html=True), name="ui")
```

**Fluxo de datos:**

```
Navegador ──GET /ui/api/config──▶ routes.py ──▶ catalog.py + Settings  (lectura, secrets enmascarados)
Navegador ──PUT /ui/api/config──▶ routes.py ──▶ schemas.py (validación) ──▶ envfile.py (backup + escritura)
                                                     └──▶ settings = Settings() en caliente (re-parse)
Navegador ──GET /ui/api/stats──▶ routes.py ──▶ registry.py (deque de RunMetrics)
Navegador ──POST /ui/api/playground──▶ routes.py ──▶ engine.run() ──SSE──▶ navegador
```

## 4. El catálogo de parámetros (corazón del diseño)

`app/web/catalog.py` define **una lista inmutable** (`tuple` de `ParamSpec`, `frozen=True`) que es la **única** fuente de verdad de la UI. El formulario se genera a partir de él; no existe ningún camino para que un parámetro "no catalogado" se pueda editar.

```python
@dataclass(frozen=True)
class ParamSpec:
    env_name: str          # clave en el .env, p. ej. "ATOMIC_PROFILE"
    field: str             # atributo en Settings, p. ej. "atomic_profile"
    group: str             # sección del formulario (coherente con .env.example)
    label: str             # etiqueta en español
    help: str              # texto de ayuda bajo el campo
    widget: str            # "select" | "toggle" | "int" | "float" | "text" | "password" | "json" | "url" | "path"
    default: Any           # default real de Settings (para "restaurar default")
    options: tuple = ()    # solo para select: opciones CERRADAS e inmutables
    minimum: float | None = None   # cota inferior (int/float)
    maximum: float | None = None   # cota superior (int/float)
    pattern: str = ""      # regex para text/url/path/model
    max_length: int = 0    # longitud máxima para text/password
    secret: bool = False   # True → nunca se devuelve en claro
    restart_required: bool = False  # True → aviso "requiere reiniciar el proxy"
```

### 4.1 Tabla completa de parámetros (especificación exacta del formulario)

**Grupo: Upstream** (el modelo real detrás del proxy)

| env | widget | opciones / validación | secret | restart |
|-----|--------|----------------------|--------|---------|
| `UPSTREAM_BASE_URL` | `url` | regex `^https?://[^\s]+$`, máx. 300 | no | no |
| `UPSTREAM_API_KEY` | `password` | máx. 500, sin `espacios/saltos de línea` (`^\S*$`); **solo escritura**: si está vacío se conserva la actual | **sí** | no |
| `UPSTREAM_MODEL` | `text` | modelo OpenAI-compat: `^[a-zA-Z0-9][a-zA-Z0-9._\-:/]{0,119}$` | no | no |

**Grupo: Proxy**

| env | widget | opciones / validación | secret | restart |
|-----|--------|----------------------|--------|---------|
| `PROXY_HOST` | `select` | cerradas: `127.0.0.1` (default), `0.0.0.0`, `localhost`, `::1` | no | **sí** |
| `PROXY_PORT` | `int` | 1024–65535 | no | **sí** |
| `REQUEST_TIMEOUT_SECONDS` | `float` | 1–600 | no | no |
| `LOG_LEVEL` | `select` | cerradas: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` | no | no |

**Grupo: Descomposición y rondas**

| env | widget | validación |
|-----|--------|-----------|
| `MAX_DECOMPOSITION_DEPTH` | `int` | 1–10 |
| `MAX_TOOL_ROUNDS_PER_PHASE` | `int` | 1–100 |

**Grupo: Presupuesto de ejecución**

| env | widget | validación |
|-----|--------|-----------|
| `MAX_SUBTASKS_PER_NODE` | `int` | 1–20 |
| `MAX_TOTAL_TASKS` | `int` | 1–200 |
| `MAX_UPSTREAM_CALLS` | `int` | 1–500 |
| `MAX_EXECUTION_SECONDS` | `float` | 10–3600 |
| `MAX_INPUT_CHARS` | `int` | 1_000–10_000_000 |
| `MAX_ACCUMULATED_CONTEXT_CHARS` | `int` | 1_000–20_000_000 |
| `MAX_PREVIOUS_RESULTS_CHARS` | `int` | 0–10_000_000 |

**Grupo: Reintentos del upstream**

| env | widget | validación |
|-----|--------|-----------|
| `UPSTREAM_MAX_RETRIES` | `int` | 0–10 |
| `UPSTREAM_RETRY_BASE_SECONDS` | `float` | 0–60 |
| `UPSTREAM_RETRY_MAX_SECONDS` | `float` | 0–300 |
| `UPSTREAM_MAX_RETRY_AFTER_SECONDS` | `float` | 0–600 |

**Grupo: Modelos y proveedores por fase** (vacío = usar los del upstream)

| env | widget | validación | secret |
|-----|--------|-----------|--------|
| `PLANNER_MODEL` / `EXECUTOR_MODEL` / `SYNTHESIS_MODEL` / `VERIFICATION_MODEL` | `text` | mismo patrón de modelo que `UPSTREAM_MODEL`; vacío permitido (= fallback) | no |
| `PLANNER_BASE_URL` / `EXECUTOR_BASE_URL` / `SYNTHESIS_BASE_URL` / `VERIFICATION_BASE_URL` | `url` | `^https?://[^\s]+$` o vacío | no |
| `PLANNER_API_KEY` / `EXECUTOR_API_KEY` / `SYNTHESIS_API_KEY` / `VERIFICATION_API_KEY` | `password` | `^\S*$`, máx. 500; vacío = conservar/sin key de fase | **sí** |
| `PLANNER_TEMPERATURE` | `float` | 0.0–2.0 | no |
| `EXECUTOR_TEMPERATURE` | `float` | 0.0–2.0 | no |
| `INTERNAL_MAX_TOKENS` | `int` | 1–1_000_000 o vacío (sin tope) | no |

**Grupo: Fast path y perfiles**

| env | widget | opciones / validación |
|-----|--------|----------------------|
| `ATOMIC_FAST_PATH` | `toggle` | on/off (bool) |
| `ALWAYS_SYNTHESIZE` | `toggle` | on/off (bool) |
| `ATOMIC_PROFILE` | `select` | **cerradas**: `fast`, `balanced` (default), `quality` |
| `QUALITY_MODEL` | `text` | patrón de modelo; vacío = no hay modelo de calidad separado |

**Grupo: Verificación y paralelismo**

| env | widget | validación |
|-----|--------|-----------|
| `ENABLE_VERIFICATION` | `toggle` | on/off |
| `VERIFICATION_MAX_REVISIONS` | `int` | 0–5 |
| `ENABLE_PARALLEL_TASKS` | `toggle` | on/off |
| `MAX_PARALLEL_TASKS` | `int` | 1–16 |

**Grupo: Sesiones**

| env | widget | validación | restart |
|-----|--------|-----------|---------|
| `SESSION_TTL_SECONDS` | `float` | 60–86400 | no |
| `MAX_SESSIONS` | `int` | 1–10000 | no |
| `SESSION_BACKEND` | `select` | **cerradas**: `memory` (default), `sqlite` | no* |
| `SESSION_DATABASE_PATH` | `path` | relativo (`./…`) o absoluto sin `..`, máx. 300, extensión `.db`/`.sqlite` | no* |

\* El backend se instancia en el arranque: si cambia, el proxy debe reiniciarse (marcar `restart_required=True` para que la UI avise).

**Grupo: Multimodal**

| env | widget | opciones |
|-----|--------|----------|
| `MULTIMODAL_FORWARDING` | `select` | **cerradas**: `always` (default), `smart`, `execution_only`, `off` |

**Grupo: Observabilidad**

| env | widget | validación | secret |
|-----|--------|-----------|--------|
| `TRACE_MODE` | `select` | **cerradas**: `off`, `summary` (default), `full` | no |
| `EXPOSE_REASONING_CONTENT` | `toggle` (tri-estado: sin definir / true / false) | legacy; si se toca, pisa `TRACE_MODE` (documentar en el help) | no |
| `EXPOSE_METRICS` | `toggle` | on/off | no |
| `MODEL_PRICES` | `json` | JSON de `Dict[str, Dict[str, float]]`, claves de precio solo `input`/`output`/`prompt`/`completion`, valores ≥ 0 (validación Pydantic, ver §5.3) | no |

**Grupo: UI**

| env | widget | validación | secret | restart |
|-----|--------|-----------|--------|---------|
| `UI_TOKEN` | `password` | `\A[A-Za-z0-9_\-]{16,128}\Z` o vacío (= sin autenticación, solo aceptable en `127.0.0.1`) | **sí** | no |

`UI_TOKEN` protege las rutas de escritura de `/ui/api` (header `X-UI-Token`). Es editable desde la propia UI, pero **cada escritura exige el token vigente** mientras haya uno definido: no se puede reconfigurar la UI sin conocerlo (y siempre se puede corregir a mano en el `.env`).

## 5. Reglas de validación (los campos que sí se escriben a mano)

Los únicos widgets de escritura libre son `text`, `password`, `url`, `path` y `json`. Todos validan **en dos capas**: patrón/`min`/`max` en el navegador (feedback inmediato) y **modelo Pydantic en el servidor** (`app/web/schemas.py`), que es el que manda. Un `PUT` inválido devuelve `422` con el detalle de campo y **no toca el `.env`**.

### 5.1 Patrones (compartidos cliente/servidor, definidos una sola vez en el catálogo)

```python
MODEL_RE   = r"^[a-zA-Z0-9][a-zA-Z0-9._\-:/]{0,119}$"      # modelos: gpt-4o, deepseek-v4-flash, org/m:tag
URL_RE     = r"^https?://[^\s]+$"                            # http(s) sin espacios
SECRET_RE  = r"^\S*$"                                        # keys: una línea, sin espacios
PATH_RE    = r"^\.{0,2}/?[^\*\?\"<>\|]{1,299}(\.(db|sqlite))?$"  # rutas de sesión
KEY_RE     = r"^[A-Za-z0-9_\-]{16,128}$"                      # UI_TOKEN (§7, Paso 7)
```

Reglas comunes:
- **Campos opcionales de fase** (`*_MODEL`, `*_BASE_URL`, `*_API_KEY`, `QUALITY_MODEL`, `INTERNAL_MAX_TOKENS`): vacío = "usar fallback / sin key / sin tope". El formulario muestra el placeholder `= valor global`.
- **Campos secretos**: el `GET` devuelve solo `"••••1234"` (4 últimos chars) o `"(no definida)"`. Si el `PUT` envía vacío o la máscara, **se conserva el valor actual**. Solo un valor nuevo real (que supere `SECRET_RE`) lo reemplaza.
- **Números**: `int` con `ge`/`le`; `float` con `ge`/`le` y normalización de coma decimal (aceptar `0,2` → rechazar con mensaje, no convertir en silencio: mejor error claro que sorpresa).
- **Booleans**: `toggle` → `true`/`false` en el `.env` (estilo del `.env.example`).

### 5.2 Ejemplo de esquema derivado del catálogo (no se escribe a mano)

```python
# app/web/schemas.py — se GENERA desde catalog.PARAMS (una sola fuente de verdad)
def build_config_model() -> type[BaseModel]:
    fields = {}
    for p in catalog.PARAMS:
        if p.widget == "select":   v = Literal[tuple(p.options)]
        elif p.widget == "toggle": v = Optional[bool]
        elif p.widget == "int":    v = Annotated[int, Field(ge=p.minimum, le=p.maximum)]
        elif p.widget == "float":  v = Annotated[float, Field(ge=p.minimum, le=p.maximum)]
        elif p.widget == "json":   v = ModelPrices            # ver 5.3
        else:                      v = Annotated[str, StringConstraints(pattern=p.pattern, max_length=p.max_length)]
        fields[p.field] = (Optional[v], None)
    return create_model("ConfigUpdate", **fields)

ConfigUpdate = build_config_model()
```

Si mañana añades un parámetro a `catalog.py`, aparece automáticamente en el formulario **y** en la validación del servidor: no hay dos sitios que mantener.

> **Nota de implementación (Paso 2):** los patrones del catálogo se aplican con `re.match` de Python en un `model_validator(mode="after")` (`_pattern_validator`), **no** con `StringConstraints(pattern=...)`: pydantic v2 valida `pattern` con el motor Rust `regex`, que no soporta `\Z` ni lookaround (`PATH_RE` usa `(?!\.\.)`) y fallaría con `SchemaError`. Misma semántica, mismo patrón, un solo origen (el catálogo).

### 5.3 `MODEL_PRICES` (JSON)

```python
class PhasePrice(BaseModel):
    model_config = ConfigDict(extra="forbid")      # nada de claves raras
    input:  Optional[Annotated[float, Field(ge=0, le=10_000)]] = None
    output: Optional[Annotated[float, Field(ge=0, le=10_000)]] = None
    prompt: Optional[Annotated[float, Field(ge=0, le=10_000)]] = None
    completion: Optional[Annotated[float, Field(ge=0, le=10_000)]] = None

class ModelPrices(BaseModel):
    root: Dict[str, PhasePrice]                     # clave = nombre de modelo
    # se serializa a {"gpt-4o": {"input": 2.5, "output": 10.0}}
```

Ejemplo válido: `{"deepseek-v4-flash": {"input": 0.5, "output": 2.0}}`. Inválido (422): `{"m": {"entrada": 1}}` → *clave desconocida `entrada`*.

## 6. API de la UI (`/ui/api`)

| Método | Ruta | Qué hace | Errores |
|--------|------|----------|---------|
| `GET` | `/ui/api/config` | Config efectiva agrupada por `group`; secrets **enmascarados**; `restart_required` por campo | — |
| `PUT` | `/ui/api/config` | Valida con `ConfigUpdate` → backup del `.env` → reescribe solo las claves enviadas → re-parsea `Settings` en caliente | `422` validación; `500` si el `.env` no se puede escribir (con backup intacto) |
| `POST` | `/ui/api/config/reset` | Body: `{"fields": ["ATOMIC_PROFILE"]}` → restaura defaults del catálogo en el `.env` | `422` si el campo no existe en el catálogo |
| `GET` | `/ui/api/catalog` | El catálogo (para que el JS genere widgets sin hardcodear nada) | — |
| `GET` | `/ui/api/stats` | Últimas N ejecuciones del `registry` + agregados: uptime, llamadas, reintentos, tokens, coste, límites, sesiones activas/pausadas | — |
| `GET` | `/ui/api/stats/stream` | `EventSource`: stats actualizados cada 2 s | — |
| `POST` | `/ui/api/playground` | Body: `{"messages": [...], "profile": "fast"}` → **SSE** con eventos `reasoning`/`content`/`tool_calls`/`metrics` del motor real | `422`; `502` si el upstream falla |
| `GET` | `/ui/api/sessions` | Lista de sesiones del store (id, estado, fase pausa, edad) | — |
| `DELETE` | `/ui/api/sessions/{id}` | Elimina una sesión | `404` |

**Seguridad transversal:** todas las rutas de `PUT/DELETE/POST` exigen header `X-UI-Token` si `settings.ui_token` está definido (`401` si falta o no coincide, comparación con `secrets.compare_digest`). `UI_TOKEN` está catalogado (widget `password`, secret, `KEY_RE`): vacío = desactivado, aceptable porque el proxy escucha en `127.0.0.1` por defecto.

**Formato del stream del playground.** El JS lo lee con `fetch` + `ReadableStream` (no `EventSource`: ese solo hace `GET` y este endpoint es `POST`). Cada evento va etiquetado y su `data` es, salvo en `metrics`/`error`, exactamente el chunk OpenAI-compatible que produce `app/sse.py`:

| evento | `data` |
|--------|--------|
| `chunk` | chunk OpenAI-compatible (rol, `finish_reason`) — mismo formato que `/v1/chat/completions` |
| `reasoning` | chunk con `delta.reasoning_content` (se omite con `TRACE_MODE=off`) |
| `content` | chunk con `delta.content` |
| `tool_calls` | chunk con `delta.tool_calls`, seguido del cierre `finish_reason="tool_calls"` |
| `metrics` | `RunMetrics.to_dict()` del run (llamadas, tokens, coste, límites alcanzados) |
| `error` | `{"message", "type"}` si el upstream falla a mitad |
| `done` | `[DONE]` |

El playground **no persiste sesión** (no llama a `_persist_session`) ni alimenta el dashboard (`registry.record` solo se llama desde `/v1/chat/completions`): es un banco de pruebas y no debe contaminar el estado de los clientes reales.



## 7. Fase 1 — implementación paso a paso

Orden estricto; cada paso deja el proyecto en verde (`pytest`) antes de pasar al siguiente.

### Paso 1 — `app/web/catalog.py` (catálogo único)

```python
# app/web/catalog.py — FUENTE ÚNICA de verdad de la UI
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

@dataclass(frozen=True)
class ParamSpec:
    key: str            # clave ENV, ej. "ATOMIC_PROFILE"
    group: str          # pestaña del formulario (§4.1)
    label: str          # etiqueta visible en español
    description: str    # texto de ayuda bajo el campo
    widget: str         # select | toggle | int | float | text | url | key | json | slider
    default: Any        # default real de Settings (verificado por test, no duplicado a ciegas)
    options: Tuple[str, ...] = ()        # widget=select: valores inmutables permitidos
    minimum: Optional[float] = None      # widget=int/float/slider
    maximum: Optional[float] = None
    secret: bool = False                 # True → se enmascara al leer, solo se reescribe si cambia
    pattern: str = ""                    # widget=text/url/key: regex Python (usa \A...\Z)
    max_length: int = 512
    restart: bool = False                # requiere reinicio del proceso
    placeholder: str = ""

PARAMS: List[ParamSpec] = [ ... ]  # tabla EXACTA de §4.1, sin desviaciones

GROUPS: List[str] = [  # orden de pestañas del formulario
    "Upstream", "Red y timeouts", "Presupuesto", "Reintentos upstream",
    "Modelos por fase", "Perfiles y fast path", "Verificación y paralelismo",
    "Multimodal", "Observabilidad", "Sesiones",
]
```

**Criterio de aceptación:** `tests/test_ui.py::test_catalog_matches_settings` recorre `Settings.model_fields` y falla si un field de `Settings` no está en `PARAMS` (o viceversa) o si `default` no coincide. Imposibilita que el catálogo y la config real diverjan.

### Paso 2 — `app/web/schemas.py` (validación derivada, §5.2)

Genera `ConfigUpdate` con `create_model` a partir de `PARAMS` (select→`Literal`, toggle→`bool`, int/float→`Field(ge, le)`, text/url/key→`StringConstraints(pattern=...)`, json→`ModelPrices`). **Prohibido** escribir reglas de validación a mano fuera del catálogo.

### Paso 3 — `app/web/envfile.py` (lectura/escritura segura del `.env`)

```python
def read_env(path) -> Dict[str, str]                      # clave=valor; conserva orden y comentarios
def write_env(path, updates: Dict[str, str]) -> None:
    # 1) backup automático: .env.bak-YYYYmmdd-HHMMSS (máx. 10, rotar)
    # 2) reescribe SOLO las claves enviadas; el resto del fichero se preserva
    # 3) fsync + os.replace (atómico: nunca un .env a medias)
def reset_fields(path, keys: List[str], catalog) -> None  # escribe default del catálogo
```

Reglas: nunca borrar líneas ajenas; clave ausente → se añade en su grupo; si el backup falla, **no se escribe nada** (500 con backup intacto).

### Paso 4 — `app/web/routes.py` (API de la §6)

```python
router = APIRouter(prefix="/ui/api", tags=["ui"])

def require_token(x_ui_token: Header(default="")) -> None:
    if settings.ui_token and not secrets.compare_digest(x_ui_token, settings.ui_token):
        raise HTTPException(401)

@router.get("/config")     # agrupa por PARAMS.group; secrets → "••••"
@router.put("/config")     # ConfigUpdate → write_env → reload_settings() en caliente
@router.post("/config/reset")
@router.get("/catalog")
@router.get("/stats")      # registry de RunMetrics (§8) + session_store
@router.get("/stats/stream")     # text/event-stream, tick 2 s
@router.post("/playground")      # reutiliza _resolve_run + SSE de main.py
@router.get("/sessions")
@router.delete("/sessions/{sid}")
```

`PUT /config`: validar (`422` con detalle de Pydantic) → `write_env` → `reload_settings()` (re-instanciar `Settings` y **revalidar que parsea**; si no parsea, restaurar backup y devolver 500) → devolver config efectiva nueva. Nunca dejar el proceso con una config a medias.

`POST /playground` **reutiliza, no duplica**: importa `_resolve_run` y el formateo SSE de `app/main.py` (extraerlos a `app/streaming.py` compartido si hace falta — Paso 6). Aplica el perfil del body (`fast|balanced|quality`, select inmutable) con `apply_profile` antes de crear el motor.

**Implementado (estado real, v1.1.1).** Además de `app/web/routes.py` con los 9 endpoints:

* `app/web/registry.py` — `RunRegistry` en memoria (agregados + últimas 50 ejecuciones). `app/main.py` lo alimenta con `_record_run(prepared)` en las tres salidas del run (no streaming, stream con `tool_calls`, stream normal). El histórico persistente es Fase 2.
* `app/session.py` — `SessionStore.snapshot()` y `SessionStore.delete()` (con override en `SqliteSessionStore`, que borra también de disco) para `/ui/api/sessions`.
* `app/main.py` — `app.include_router(ui_router)` y la ruta `ENV_PATH` vive en el módulo de rutas, no en `Settings`.
* El perfil del playground lo aplica `_resolve_run(..., profile=body.profile)` (que ya llama a `resolve_runtime`), no se duplica la lógica de `profiles.py`.

Decisiones de implementación que no estaban escritas arriba y conviene no volver a discutir:

1. **`routes.ENV_PATH`** (module-level, `Path(".env")`) es el fichero que edita la UI. Los tests lo apuntan a `tmp_path` con `monkeypatch`, así la suite nunca toca el `.env` real del desarrollador.
2. **`reload_settings(env_file=".env")`** acepta la ruta a recargar (la UI pasa `str(ENV_PATH)`); `Settings(_env_file=…)`. El rollback usa `envfile.restore(path, texto_previo)`, y `envfile.write_env` ahora **devuelve la ruta del backup** que creó (o `None` si no había fichero).
3. **Secrets**: un valor vacío o la máscara (`••••1234` / `(no definida)`) **no** se reescribe (§5.1). Para borrar un secreto se usa `/config/reset`, que sí escribe el default del catálogo.
4. **`GET /stats/stream`** emite `event: stats` con tick acotado (0.25–60 s, default 2 s) y el generador se expone como `routes.stats_events(interval)` para poder testearlo sin HTTP.
5. El panel estático (`/ui/`) y la redirección de `/` siguen siendo el Paso 6.


### Paso 5 — `app/web/static/` (frontend sin build)

```
app/web/static/
  index.html     # 3 pestañas: Configuración · Dashboard · Playground (+ Sesiones en Fase 2)
  app.js         # vanilla JS: fetch del catálogo → genera los widgets; sin framework
  styles.css     # tema oscuro, responsive, sin CDN ni dependencias externas
```

- **Los widgets se generan desde `GET /ui/api/catalog`**: el HTML no tiene ningún input hardcodeado — si el catálogo dice `select`, el JS pinta un `<select>` con exactamente esas opciones; si `toggle`, un switch; etc. El usuario **nunca ve un campo libre** donde el catálogo define opciones.
- Campo `secret`: muestra `••••••`; solo envía el valor si el usuario lo reescribe; botón "mostrar" opcional.
- Errores `422`: se pintan bajo el campo concreto (el validador devuelve `loc` → campo).
- `EventSource` para `/ui/api/stats/stream` (dashboard) y para la respuesta del playground (SSE del motor).
- Tras un `PUT` correcto: toast "Guardado · aplicado en caliente" o "Guardado · requiere reinicio" según `restart_required` de cada campo.

### Paso 6 — cableado en `app/main.py`

```python
from app.web.routes import router as ui_router
app.include_router(ui_router)
app.mount("/ui", StaticFiles(directory=Path(__file__).parent / "web" / "static", html=True), name="ui")
```

Y extraer a `app/streaming.py` (si no estaba ya): `format_sse(event, ...)` y el bucle que convierte `engine.run()/resume()` en chunks SSE, compartido entre `/v1/chat/completions` y `POST /ui/api/playground`. `GET /` redirige a `/ui/`. **No** se toca la lógica del proxy: la UI solo consume la misma orquestación existente.

### Paso 7 — configuración de la propia UI (catálogo)

Añadido a `catalog.PARAMS` y `Settings`: `UI_TOKEN` (widget `password`, secret, patrón `KEY_RE` = `\A[A-Za-z0-9_\-]{16,128}\Z`, opcional; grupo `UI` en `GROUPS`). Mientras el proxy escucha en `127.0.0.1` puede quedar vacío; **es obligatorio** si algún día se expone en red (así lo indica su `description`). La sección `UI` del `.env.example` ya existe, de modo que una clave ausente se inserta en su sitio (este token se escribe a mano en el `.env` del mismo modo que el resto).

Pendiente de este paso: `test_token_required_when_set` (depende del Paso 4, que es quien exige el header).


### Paso 8 — tests (`tests/test_ui.py`)

| Test | Qué protege |
|------|-------------|
| `test_catalog_matches_settings` | catálogo ≡ `Settings` (fields + defaults) |
| `test_select_rejects_invalid_value` | `PUT` con `ATOMIC_PROFILE="turbo"` → `422` |
| `test_int_bounds` | `MAX_TOTAL_TASKS=0` o `=9999` → `422` |
| `test_free_text_patterns` | `UPSTREAM_BASE_URL="no url"` → `422`; API key con espacios → `422` |
| `test_put_writes_env_and_reloads` | `PUT` válido → `.env` cambiado, backup creado, `settings` recargado |
| `test_put_invalid_keeps_env_intact` | `422` → el `.env` byte a byte igual |
| `test_secret_masked_on_get` | la API key nunca sale en claro en `GET /config` |
| `test_model_prices_schema` | JSON con clave desconocida → `422` |
| `test_token_required_when_set` | con `UI_TOKEN` definido, `PUT` sin header → `401` |
| `test_playground_streams` | SSE del playground emite `reasoning`+`content` con el fake upstream |

### Paso 9 — documentación

- README: captura + enlace a `docs/UI_IMPLEMENTACION.md`.
- `GUIA_DEL_PROXY.md` §config: "la misma tabla editable desde la UI en `/ui/`".
- `.env.example`: añadir `UI_TOKEN=`.

## 8. Fase 2 — si la UI se usa de verdad

1. **Explorador de sesiones** (antes era Fase 2 ya definida): tabla de `GET /ui/api/sessions` con estado (`completada`/`pausada en <fase>`), edad vs TTL, nº de resultados; detalle en panel lateral: árbol de tareas (`root.to_dict()`), resultados por hoja, y botón de borrado (`DELETE`). Opcional: botón "reanudar manualmente" solo si `pending_phase` no es nulo y con confirmación.
2. **Selector de perfil visible** en la cabecera de la UI (aplica `ATOMIC_PROFILE` vía `PUT /config`) — el mismo select inmutable de 3 opciones; al cambiarlo el dashboard marca el perfil activo.
3. **Editor avanzado de modelos por fase**: los 4 modelos + 4 base_urls + 4 api_keys ya existen como campos en Fase 1; aquí se añade un test de conexión por fase (llamada mínima `GET /models` o `POST` de 1 token) con semáforo verde/rojo, sin exponer las keys.
4. **Log en vivo**: panel con `EventSource` sobre una cola de los eventos `log()` estructurados (JSON) del proceso, filtrable por nivel (`LOG_LEVEL`) y por `request_id`.
5. **Histórico de métricas persistido**: hoy `RunMetrics` es por ejecución y volátil; persistir agregados por día (SQLite o JSON) para gráficas de coste/tokens a lo largo del tiempo.

Cada punto de Fase 2 sigue las mismas reglas: catálogo único, widgets generados, validación en servidor, sin duplicar lógica del proxy.

## 9. Reanudación — checklist de progreso

> **Si este documento quedó a medias, esta sección es tu punto de reanudación.** Marca lo que esté hecho; todo lo no marcado es lo siguiente. Regla de oro: **nada nuevo sin `pytest` en verde** y **nada de la UI escrito a mano que deba salir del catálogo**.

**Estado actual: pasos 1–4 y 7 implementados y en verde (69/69 tests); los 10 tests del Paso 8 ya existen en `tests/test_ui.py`. Falta el frontend (Pasos 5–6) y la documentación final (Paso 9).** Índice de progreso = Pasos 1–9 de §7 + Fase 2.

- [x] **Paso 1** — `app/web/catalog.py` existe; `PARAMS` cubre exactamente `Settings.model_fields`; test `test_catalog_matches_settings` en verde.
- [x] **Paso 2** — `app/web/schemas.py` genera `ConfigUpdate` desde el catálogo; test `test_config_update_*` (Literal, límites, patrones, secrets, modelo precios) en verde.
- [x] **Paso 3** — `app/web/envfile.py`: backup atómico + reescritura selectiva; tests `test_read_env_*`, `test_write_env_*` y `test_reset_fields_*` en verde.
- [x] **Paso 4** — `app/web/routes.py` con los 9 endpoints de §6 montados en `main.py`; `PUT` recarga en caliente y revierte ante config inválida. Incluye `app/web/registry.py` (dashboard) y `snapshot()`/`delete()` en el store de sesiones.
- [ ] **Paso 5** — `static/index.html` + `app.js` + `styles.css`; **verificación manual**: abrir `/ui/`, cambiar `ATOMIC_PROFILE` en el desplegable y comprobar que no existe ningún campo de texto libre donde el catálogo dice `select`/`toggle`/`slider`.
- [ ] **Paso 6** — montar el estático en `/ui/` (la API ya está montada desde el Paso 4) + redirección de `/` y SSE compartido si hace falta extraerlo a `app/streaming.py`; `/v1/chat/completions` sigue pasando sus tests (nada roto).
- [x] **Paso 7** — `UI_TOKEN` en `Settings` + catálogo + `.env.example`; `test_token_required_when_set` en verde (se escribe con el Paso 4, que es quien exige el header).
- [x] **Paso 8** — los 10 tests de la tabla en verde en `tests/test_ui.py` (los nombres pueden variar: `test_config_update_enforces_catalog_rules`, `test_put_config_writes_env_reloads_and_backs_up`, `test_put_invalid_value_leaves_env_byte_identical`, `test_get_config_groups_every_param_and_masks_secrets`, `test_config_update_model_prices`, `test_token_required_when_set`, `test_playground_streams_real_engine_events`, `test_sessions_endpoints`, `test_stats_endpoint_aggregates_registry`, `test_stats_stream_emits_named_event`).
- [ ] **Paso 9** — README y `GUIA_DEL_PROXY.md` actualizados (el `.env.example` ya tiene `UI_TOKEN` desde el Paso 7).

- [ ] **Fase 2** — §8, uno a uno (sesiones → perfil → test de conexión → log → histórico).

**How to resume (primeros 5 minutos):**
1. `python -m pytest -q` — si no son verdes, arregla la rama antes que nada.
2. Busca el primer checkbox sin marcar de §9 → ese es tu Paso N; léete su descripción en §7.
3. Relee §4.1 (tabla de parámetros) y §5 (validación) antes de tocar cualquier campo: **esa es la restricción inmutable del proyecto** — cada parámetro con sus opciones ya configuradas; texto libre solo con el patrón del catálogo.
4. Al terminar el paso: `pytest` en verde → marca el checkbox → commit de ese paso solo (un paso = un commit, mensaje `Add UI step N: <qué>`).

## 10. Definición de hecho (Fase 1)

La Fase 1 está terminada cuando **todo** esto es cierto:

1. `GET /ui/` sirve la UI desde el propio proxy (mismo proceso, sin build).
2. **Todos** los parámetros de `.env`/`Settings` se editan desde la UI; los `select`/`toggle`/`slider` **no admiten** valores fuera de opciones/rango en cliente **ni** en servidor (verificado por test con `422`).
3. Los campos de texto libre rechazan lo inválido según el patrón del catálogo (URL, API key, JSON de precios) y el error se muestra bajo el campo.
4. Un `PUT` válido sobrevive a un reinicio del proxy (está en el `.env`) y se aplica sin reinicio; un `PUT` inválido no deja rastro (`.env` intacto).
5. Los secrets jamás salen en claro por `GET`.
6. El dashboard muestra, en vivo: uptime, llamadas/reintentos por fase, tokens, coste, límites alcanzados y sesiones activas/pausadas.
7. El playground reproduce el flujo real del motor (descomposición → ejecución → síntesis) en streaming, reutilizando el mismo código que `/v1/chat/completions`.
8. La suite completa (existentes + `test_ui.py`) está en verde y no se ha duplicado lógica del proxy.

Fuera de alcance consciente (no bloquea Fase 1): auth de usuarios múltiples, HTTPS, acceso remoto, plugins, i18n, tema claro. Si el proxy se expone fuera de `127.0.0.1`, `UI_TOKEN` pasa a obligatorio (Paso 7).