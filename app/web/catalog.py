"""Catálogo único de parámetros de la UI (fuente única de verdad).

Especificación: docs/UI_IMPLEMENTACION.md §4 (ParamSpec + tabla 4.1) y §5.1
(patrones). El formulario de la UI y la validación del servidor
(app/web/schemas.py) se derivan de esta lista; no existe ningún parámetro
editable fuera de ella.

El test tests/test_ui.py::test_catalog_matches_settings verifica que este
catálogo y app.config.Settings no divergan: mismos campos, mismos defaults.
"""
from dataclasses import dataclass
from typing import Any, Optional, Tuple


@dataclass(frozen=True)
class ParamSpec:
    key: str                 # clave en el .env, ej. "ATOMIC_PROFILE"
    field: str               # atributo en Settings, ej. "atomic_profile"
    group: str               # pestaña del formulario (§4.1)
    label: str               # etiqueta visible en español
    description: str         # ayuda que se muestra bajo el campo
    widget: str              # select|toggle|int|float|text|password|url|path|json
    default: Any             # default real de Settings (lo verifica el test)
    options: Tuple[str, ...] = ()       # widget=select: valores CERRADOS e inmutables
    minimum: Optional[float] = None     # widget=int/float: cota inferior
    maximum: Optional[float] = None     # widget=int/float: cota superior
    pattern: str = ""                  # text/url/password/path: regex (\A...\Z)
    max_length: int = 512
    secret: bool = False               # True → se enmascara al leer; vacío al
                                       # escribir conserva el valor actual
    restart_required: bool = False     # True → aviso "requiere reiniciar el proxy"
    placeholder: str = ""


# §5.1 — patrones compartidos cliente/servidor, definidos una sola vez aquí.
MODEL_RE = r"\A[a-zA-Z0-9][a-zA-Z0-9._\-:/]{0,119}\Z"   # gpt-4o, deepseek-v4-flash, org/m:tag
URL_RE = r"\Ahttps?://[^\s]+\Z"                          # http(s) sin espacios
SECRET_RE = r"\A\S*\Z"                                   # keys: una sola línea, sin espacios
# §4.1: relativo (./…) o absoluto, SIN "..", terminado en .db/.sqlite,
# sin caracteres comodines de shell ni saltos de línea.
PATH_RE = r"\A(?!\.\.)(?:\./|/)?[^*?\"<>|\r\n]{1,297}\.(?:db|sqlite)\Z"
# §7 Paso 7: token de la UI admin. Vacío = sin autenticación (solo aceptable
# escuchando en 127.0.0.1); si se define, 16-128 chars de [A-Za-z0-9_-].
KEY_RE = r"\A[A-Za-z0-9_\-]{16,128}\Z"

WIDGETS: Tuple[str, ...] = (
    "select", "toggle", "int", "float", "text", "password", "url", "path", "json",
)

GROUPS: Tuple[str, ...] = (  # orden de las pestañas del formulario (§4.1)
    "Upstream",
    "Proxy",
    "Descomposición y rondas",
    "Presupuesto de ejecución",
    "Reintentos del upstream",
    "Modelos y proveedores por fase",
    "Fast path y perfiles",
    "Verificación y paralelismo",
    "Sesiones",
    "Multimodal",
    "Observabilidad",
    "UI",
)

PARAMS: Tuple[ParamSpec, ...] = (
    # --- Grupo: Upstream ---
    ParamSpec(
        key="UPSTREAM_BASE_URL", field="upstream_base_url", group="Upstream",
        label="URL base del upstream",
        description="Endpoint real al que el proxy reenvía las llamadas internas (compatible con OpenAI).",
        widget="url", default="https://api.deepseek.com", pattern=URL_RE, max_length=300,
    ),
    ParamSpec(
        key="UPSTREAM_API_KEY", field="upstream_api_key", group="Upstream",
        label="API key del upstream",
        description="Clave de acceso al proveedor. Solo escritura: si se envía vacía, se conserva la actual.",
        widget="password", default="", pattern=SECRET_RE, max_length=500, secret=True,
    ),
    ParamSpec(
        key="UPSTREAM_MODEL", field="upstream_model", group="Upstream",
        label="Modelo del upstream",
        description="Modelo por defecto usado por el proxy y como fallback de las fases sin modelo propio.",
        widget="text", default="deepseek-v4-flash", pattern=MODEL_RE, max_length=120,
    ),
    # --- Grupo: Proxy ---
    ParamSpec(
        key="PROXY_HOST", field="proxy_host", group="Proxy",
        label="Host de escucha",
        description="Interfaz en la que escucha el proxy. 127.0.0.1 solo accesible desde tu máquina.",
        widget="select", default="127.0.0.1",
        options=("127.0.0.1", "0.0.0.0", "localhost", "::1"), restart_required=True,
    ),
    ParamSpec(
        key="PROXY_PORT", field="proxy_port", group="Proxy",
        label="Puerto",
        description="Puerto TCP del servidor. Requiere reiniciar el proxy.",
        widget="int", default=8000, minimum=1024, maximum=65535, restart_required=True,
    ),
    ParamSpec(
        key="REQUEST_TIMEOUT_SECONDS", field="request_timeout_seconds", group="Proxy",
        label="Timeout de petición (s)",
        description="Segundos máximos que el proxy espera una respuesta del upstream antes de dar error.",
        widget="float", default=120.0, minimum=1.0, maximum=600.0,
    ),
    ParamSpec(
        key="LOG_LEVEL", field="log_level", group="Proxy",
        label="Nivel de log",
        description="Nivel de detalle de los logs estructurados JSON del proxy.",
        widget="select", default="INFO",
        options=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    ),
    # --- Grupo: Descomposición y rondas ---
    ParamSpec(
        key="MAX_DECOMPOSITION_DEPTH", field="max_decomposition_depth", group="Descomposición y rondas",
        label="Profundidad máxima de descomposición",
        description="Niveles máximos de subdivisiones del árbol de tareas antes de forzar la atomicidad.",
        widget="int", default=3, minimum=1, maximum=10,
    ),
    ParamSpec(
        key="MAX_TOOL_ROUNDS_PER_PHASE", field="max_tool_rounds_per_phase", group="Descomposición y rondas",
        label="Rondas de herramientas por fase",
        description="Límite de llamadas encadenadas a tools dentro de una hoja o la síntesis; al superarlo se fuerza texto.",
        widget="int", default=25, minimum=1, maximum=100,
    ),
    # --- Grupo: Presupuesto de ejecución ---
    ParamSpec(
        key="MAX_SUBTASKS_PER_NODE", field="max_subtasks_per_node", group="Presupuesto de ejecución",
        label="Subtareas por nodo",
        description="Máximo de subtareas que un nodo puede generar en un nivel de descomposición.",
        widget="int", default=6, minimum=1, maximum=20,
    ),
    ParamSpec(
        key="MAX_TOTAL_TASKS", field="max_total_tasks", group="Presupuesto de ejecución",
        label="Tareas totales",
        description="Máximo de tareas atómicas en todo el árbol de una ejecución.",
        widget="int", default=25, minimum=1, maximum=200,
    ),
    ParamSpec(
        key="MAX_UPSTREAM_CALLS", field="max_upstream_calls", group="Presupuesto de ejecución",
        label="Llamadas al upstream",
        description="Máximo de llamadas al proveedor por ejecución (incluye reintentos).",
        widget="int", default=40, minimum=1, maximum=500,
    ),
    ParamSpec(
        key="MAX_EXECUTION_SECONDS", field="max_execution_seconds", group="Presupuesto de ejecución",
        label="Tiempo máximo de ejecución (s)",
        description="Segundos totales que puede durar un run antes de cortarse con nota de presupuesto.",
        widget="float", default=300.0, minimum=10.0, maximum=3600.0,
    ),
    ParamSpec(
        key="MAX_INPUT_CHARS", field="max_input_chars", group="Presupuesto de ejecución",
        label="Caracteres de entrada máximos",
        description="Rechaza peticiones cuyo historial de entrada supere este tamaño.",
        widget="int", default=200_000, minimum=1_000, maximum=10_000_000,
    ),
    ParamSpec(
        key="MAX_ACCUMULATED_CONTEXT_CHARS", field="max_accumulated_context_chars",
        group="Presupuesto de ejecución",
        label="Contexto acumulado máx.",
        description="Tope de caracteres acumulados que se inyectan en las fases internas.",
        widget="int", default=300_000, minimum=1_000, maximum=20_000_000,
    ),
    ParamSpec(
        key="MAX_PREVIOUS_RESULTS_CHARS", field="max_previous_results_chars",
        group="Presupuesto de ejecución",
        label="Resultados previos máx.",
        description="Recorte del bloque de resultados previos que ve cada tarea atómica.",
        widget="int", default=50_000, minimum=0, maximum=10_000_000,
    ),
    # --- Grupo: Reintentos del upstream ---
    ParamSpec(
        key="UPSTREAM_MAX_RETRIES", field="upstream_max_retries", group="Reintentos del upstream",
        label="Reintentos máximos",
        description="Intentos extra ante 5xx/429 o errores de red (backoff exponencial con jitter).",
        widget="int", default=3, minimum=0, maximum=10,
    ),
    ParamSpec(
        key="UPSTREAM_RETRY_BASE_SECONDS", field="upstream_retry_base_seconds",
        group="Reintentos del upstream",
        label="Backoff base (s)",
        description="Segundos iniciales del esperado entre reintentos (se duplica cada intento).",
        widget="float", default=1.0, minimum=0.0, maximum=60.0,
    ),
    ParamSpec(
        key="UPSTREAM_RETRY_MAX_SECONDS", field="upstream_retry_max_seconds",
        group="Reintentos del upstream",
        label="Backoff máximo (s)",
        description="Techo del esperado calculado por el backoff exponencial.",
        widget="float", default=10.0, minimum=0.0, maximum=300.0,
    ),
    ParamSpec(
        key="UPSTREAM_MAX_RETRY_AFTER_SECONDS", field="upstream_max_retry_after_seconds",
        group="Reintentos del upstream",
        label="Techo de Retry-After (s)",
        description="Máximo que se respeta si el upstream envía la cabecera Retry-After.",
        widget="float", default=30.0, minimum=0.0, maximum=600.0,
    ),
    # --- Grupo: Modelos y proveedores por fase (vacío = usar los del upstream) ---
    ParamSpec(
        key="PLANNER_MODEL", field="planner_model", group="Modelos y proveedores por fase",
        label="Modelo del planificador",
        description="Modelo de la Fase 1 (descomposición). Vacío = modelo del upstream.",
        widget="text", default="", pattern=MODEL_RE, max_length=120,
    ),
    ParamSpec(
        key="EXECUTOR_MODEL", field="executor_model", group="Modelos y proveedores por fase",
        label="Modelo del ejecutor",
        description="Modelo de la Fase 2 (tareas atómicas). Vacío = modelo del upstream.",
        widget="text", default="", pattern=MODEL_RE, max_length=120,
    ),
    ParamSpec(
        key="SYNTHESIS_MODEL", field="synthesis_model", group="Modelos y proveedores por fase",
        label="Modelo de síntesis",
        description="Modelo de la Fase 3 (respuesta final). Vacío = modelo del upstream.",
        widget="text", default="", pattern=MODEL_RE, max_length=120,
    ),
    ParamSpec(
        key="VERIFICATION_MODEL", field="verification_model", group="Modelos y proveedores por fase",
        label="Modelo de verificación",
        description="Modelo de la verificación final opcional. Vacío = modelo del upstream.",
        widget="text", default="", pattern=MODEL_RE, max_length=120,
    ),
    ParamSpec(
        key="PLANNER_BASE_URL", field="planner_base_url", group="Modelos y proveedores por fase",
        label="Base URL del planificador",
        description="Proveedor distinto para la Fase 1. Vacío = base URL del upstream.",
        widget="url", default="", pattern=URL_RE, max_length=300,
    ),
    ParamSpec(
        key="EXECUTOR_BASE_URL", field="executor_base_url", group="Modelos y proveedores por fase",
        label="Base URL del ejecutor",
        description="Proveedor distinto para la Fase 2. Vacío = base URL del upstream.",
        widget="url", default="", pattern=URL_RE, max_length=300,
    ),
    ParamSpec(
        key="SYNTHESIS_BASE_URL", field="synthesis_base_url", group="Modelos y proveedores por fase",
        label="Base URL de síntesis",
        description="Proveedor distinto para la Fase 3. Vacío = base URL del upstream.",
        widget="url", default="", pattern=URL_RE, max_length=300,
    ),
    ParamSpec(
        key="VERIFICATION_BASE_URL", field="verification_base_url",
        group="Modelos y proveedores por fase",
        label="Base URL de verificación",
        description="Proveedor distinto para la verificación. Vacío = base URL del upstream.",
        widget="url", default="", pattern=URL_RE, max_length=300,
    ),
    ParamSpec(
        key="PLANNER_API_KEY", field="planner_api_key", group="Modelos y proveedores por fase",
        label="API key del planificador",
        description="Key de la Fase 1 si usa otro proveedor. Vacío = sin key de fase.",
        widget="password", default="", pattern=SECRET_RE, max_length=500, secret=True,
    ),
    ParamSpec(
        key="EXECUTOR_API_KEY", field="executor_api_key", group="Modelos y proveedores por fase",
        label="API key del ejecutor",
        description="Key de la Fase 2 si usa otro proveedor. Vacío = sin key de fase.",
        widget="password", default="", pattern=SECRET_RE, max_length=500, secret=True,
    ),
    ParamSpec(
        key="SYNTHESIS_API_KEY", field="synthesis_api_key", group="Modelos y proveedores por fase",
        label="API key de síntesis",
        description="Key de la Fase 3 si usa otro proveedor. Vacío = sin key de fase.",
        widget="password", default="", pattern=SECRET_RE, max_length=500, secret=True,
    ),
    ParamSpec(
        key="VERIFICATION_API_KEY", field="verification_api_key",
        group="Modelos y proveedores por fase",
        label="API key de verificación",
        description="Key de la verificación si usa otro proveedor. Vacío = sin key de fase.",
        widget="password", default="", pattern=SECRET_RE, max_length=500, secret=True,
    ),
    ParamSpec(
        key="PLANNER_TEMPERATURE", field="planner_temperature",
        group="Modelos y proveedores por fase",
        label="Temperatura del planificador",
        description="Determinismo de la descomposición (0 = siempre decide igual).",
        widget="float", default=0.0, minimum=0.0, maximum=2.0,
    ),
    ParamSpec(
        key="EXECUTOR_TEMPERATURE", field="executor_temperature",
        group="Modelos y proveedores por fase",
        label="Temperatura del ejecutor",
        description="Creatividad de las tareas atómicas.",
        widget="float", default=0.2, minimum=0.0, maximum=2.0,
    ),
    ParamSpec(
        key="INTERNAL_MAX_TOKENS", field="internal_max_tokens",
        group="Modelos y proveedores por fase",
        label="Máx. de tokens internos",
        description="Tope de tokens por llamada interna. Vacío = sin tope.",
        widget="int", default=None, minimum=1, maximum=1_000_000,
    ),
    # --- Grupo: Fast path y perfiles ---
    ParamSpec(
        key="ATOMIC_FAST_PATH", field="atomic_fast_path", group="Fast path y perfiles",
        label="Fast path",
        description="Si la Fase 1 decide 'atómica', va directo a síntesis saltando el árbol.",
        widget="toggle", default=False,
    ),
    ParamSpec(
        key="ALWAYS_SYNTHESIZE", field="always_synthesize", group="Fast path y perfiles",
        label="Síntesis siempre",
        description="Fuerza la Fase 3 aunque solo haya una tarea.",
        widget="toggle", default=False,
    ),
    ParamSpec(
        key="ATOMIC_PROFILE", field="atomic_profile", group="Fast path y perfiles",
        label="Perfil de ejecución",
        description="Ajuste predefinido de coste/calidad (ver app/profiles.py).",
        widget="select", default="balanced",
        options=("fast", "balanced", "quality"),
    ),
    ParamSpec(
        key="QUALITY_MODEL", field="quality_model", group="Fast path y perfiles",
        label="Modelo de calidad",
        description="Modelo premium usado en el perfil quality. Vacío = no hay separado.",
        widget="text", default="", pattern=MODEL_RE, max_length=120,
    ),
    # --- Grupo: Verificación y paralelismo ---
    ParamSpec(
        key="ENABLE_VERIFICATION", field="enable_verification", group="Verificación y paralelismo",
        label="Verificación final",
        description="Añade una Fase 4 opcional que revisa la síntesis y la corrige (hasta VERIFICATION_MAX_REVISIONS).",
        widget="toggle", default=False,
    ),
    ParamSpec(
        key="VERIFICATION_MAX_REVISIONS", field="verification_max_revisions", group="Verificación y paralelismo",
        label="Revisiones máximas",
        description="Número máximo de correcciones que aplica la verificación antes de entregar.",
        widget="int", default=1, minimum=0, maximum=5,
    ),
    ParamSpec(
        key="ENABLE_PARALLEL_TASKS", field="enable_parallel_tasks", group="Verificación y paralelismo",
        label="Tareas en paralelo",
        description="Ejecuta en una misma ronda las subtareas sin dependencias entre sí (solo si no hay tools con efectos secundarios).",
        widget="toggle", default=False,
    ),
    ParamSpec(
        key="MAX_PARALLEL_TASKS", field="max_parallel_tasks", group="Verificación y paralelismo",
        label="Máx. tareas en paralelo",
        description="Tope de tareas concurrentes por ronda.",
        widget="int", default=4, minimum=1, maximum=16,
    ),
    # --- Grupo: Sesiones ---
    ParamSpec(
        key="SESSION_TTL_SECONDS", field="session_ttl_seconds", group="Sesiones",
        label="TTL de sesión (s)",
        description="Cuánto vive una sesión sin actividad antes de expirar (reanudación y turnos consecutivos).",
        widget="float", default=1800.0, minimum=60.0, maximum=86400.0,
    ),
    ParamSpec(
        key="MAX_SESSIONS", field="max_sessions", group="Sesiones",
        label="Máx. sesiones",
        description="Límite de sesiones en memoria; al superarse se descarta la menos usada (LRU).",
        widget="int", default=200, minimum=1, maximum=10000,
    ),
    ParamSpec(
        key="SESSION_BACKEND", field="session_backend", group="Sesiones",
        label="Backend de sesiones",
        description="Dónde se persisten las sesiones. 'sqlite' sobrevive a reinicios del proxy.",
        widget="select", default="memory",
        options=("memory", "sqlite"), restart_required=True,
    ),
    ParamSpec(
        key="SESSION_DATABASE_PATH", field="session_database_path", group="Sesiones",
        label="Ruta de la base de sesiones",
        description="Fichero SQLite (solo con backend 'sqlite'). Relativo (./…) o absoluto, sin '..', terminado en .db/.sqlite.",
        widget="path", default="./data/sessions.db", pattern=PATH_RE, max_length=300,
        placeholder="./data/sessions.db", restart_required=True,
    ),
    # --- Grupo: Multimodal ---
    ParamSpec(
        key="MULTIMODAL_FORWARDING", field="multimodal_forwarding", group="Multimodal",
        label="Reenvío de imágenes",
        description="always: a todas las fases · smart: solo a las que las necesitan · execution_only: solo a la ejecución · off: nunca.",
        widget="select", default="always",
        options=("always", "smart", "execution_only", "off"),
    ),
    # --- Grupo: Observabilidad ---
    ParamSpec(
        key="TRACE_MODE", field="trace_mode", group="Observabilidad",
        label="Trazas (reasoning_content)",
        description="off: sin trazas · summary: solo eventos de fase · full: detalle interno. Solo afecta a lo que ve el cliente.",
        widget="select", default="summary",
        options=("off", "summary", "full"),
    ),
    ParamSpec(
        key="EXPOSE_REASONING_CONTENT", field="expose_reasoning_content", group="Observabilidad",
        label="Expón reasoning_content (legacy)",
        description="Variable histórica: sin definir = usar TRACE_MODE; definida (true/false) pisa TRACE_MODE por compatibilidad.",
        widget="toggle", default=None,
        placeholder="sin definir (usar TRACE_MODE)",
    ),
    ParamSpec(
        key="EXPOSE_METRICS", field="expose_metrics", group="Observabilidad",
        label="Métricas en respuestas",
        description="Incluye las cabeceras X-Atomic-* con llamadas, tokens y coste estimado.",
        widget="toggle", default=True,
    ),
    ParamSpec(
        key="MODEL_PRICES", field="model_prices", group="Observabilidad",
        label="Precios por modelo",
        description='JSON {"modelo": {"input": USD/Mtok, "output": USD/Mtok}} para el coste estimado. Claves admitidas: input/output/prompt/completion.',
        widget="json", default={},
    ),
    # --- Grupo: UI (§7, Paso 7) ---
    ParamSpec(
        key="UI_TOKEN", field="ui_token", group="UI",
        label="Token de la UI admin",
        description="Secreto que protege /ui/api (header X-UI-Token). Vacío = sin autenticación: solo aceptable si el proxy escucha en 127.0.0.1; obligatorio si se expone en red. 16-128 caracteres.",
        widget="password", default="", pattern=KEY_RE, max_length=128, secret=True,
        placeholder="vacío = sin autenticación (solo en 127.0.0.1)",
    ),
)
