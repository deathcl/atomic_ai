from __future__ import annotations

import os
from typing import Dict, Literal, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    upstream_base_url: str = "https://api.deepseek.com"
    upstream_api_key: str = ""
    upstream_model: str = "deepseek-v4-flash"

    max_decomposition_depth: int = 3
    max_tool_rounds_per_phase: int = 25

    proxy_host: str = "127.0.0.1"
    proxy_port: int = 8000

    request_timeout_seconds: float = 120.0

    session_ttl_seconds: float = 1800.0
    max_sessions: int = 200

    # "full" si EXPOSE_REASONING_CONTENT=true, "off" si false, TRACE_MODE si no se define.
    expose_reasoning_content: Optional[bool] = None
    trace_mode: Literal["off", "summary", "full"] = "summary"

    # --- Presupuesto global de ejecución (V1.1) ---
    max_subtasks_per_node: int = 6
    max_total_tasks: int = 25
    max_upstream_calls: int = 40
    max_execution_seconds: float = 300.0
    max_input_chars: int = 200_000
    max_accumulated_context_chars: int = 300_000

    # --- Reintentos del upstream (V1.1) ---
    upstream_max_retries: int = 3
    upstream_retry_base_seconds: float = 1.0
    upstream_retry_max_seconds: float = 10.0
    upstream_max_retry_after_seconds: float = 30.0

    # --- Modelos y proveedores por fase (V1.2) ---
    planner_model: str = ""
    executor_model: str = ""
    synthesis_model: str = ""
    verification_model: str = ""
    planner_base_url: str = ""
    executor_base_url: str = ""
    synthesis_base_url: str = ""
    verification_base_url: str = ""
    planner_api_key: str = ""
    executor_api_key: str = ""
    synthesis_api_key: str = ""
    verification_api_key: str = ""

    # Temperaturas internas: la respuesta final usa las del cliente si existen.
    planner_temperature: Optional[float] = 0.0
    executor_temperature: Optional[float] = 0.2
    internal_max_tokens: Optional[int] = None

    # --- Fast path para tareas atómicas (V1.2) ---
    atomic_fast_path: bool = False
    always_synthesize: bool = False

    # --- Verificación final opcional (V1.3) ---
    enable_verification: bool = False
    verification_max_revisions: int = 1

    # --- Planes con dependencias y paralelización (V1.3) ---
    enable_parallel_tasks: bool = False
    max_parallel_tasks: int = 4

    # --- Perfiles de ejecución (V1.3) ---
    atomic_profile: Literal["fast", "balanced", "quality"] = "balanced"
    quality_model: str = ""

    # --- Compresión de contexto y multimodal (V1.2) ---
    max_previous_results_chars: int = 50_000
    multimodal_forwarding: Literal["always", "smart", "execution_only", "off"] = "always"

    # --- Observabilidad (V1.1/V1.2) ---
    expose_metrics: bool = True
    model_prices: Dict[str, Dict[str, float]] = {}
    log_level: str = "INFO"

    # --- UI (V2.0) ---
    # Secreto de autorización de la UI admin (header X-UI-Token). Si está
    # vacío no se exige (el proxy por defecto escucha en 127.0.0.1);
    # obligatorio si se expone en red. Editable desde la propia UI, pero
    # cada escritura exige el token VIGENTE (si ya hay uno definido), de
    # modo que no se puede reconfigurar la UI sin conocerlo.
    ui_token: str = ""

    # --- Persistencia de sesiones (V1.3) ---
    session_backend: Literal["memory", "sqlite"] = "memory"
    session_database_path: str = "./data/sessions.db"

    def resolved_api_key(self) -> str:
        return self.upstream_api_key or os.environ.get("DEEPSEEK_API_KEY", "")

    def effective_trace_mode(self) -> str:
        """TRACE_MODE, salvo que EXPOSE_REASONING_CONTENT esté definido (compatibilidad).

        La variable histórica sigue mandando cuando existe para no cambiar el
        comportamiento de instalaciones que ya la tenían configurada.
        """
        if self.expose_reasoning_content is not None:
            return "full" if self.expose_reasoning_content else "off"
        return self.trace_mode


settings = Settings()


def reload_settings(env_file: str = ".env") -> Settings:
    """Recarga la configuración desde el ``.env`` YA actualizado y la
    propaga al objeto `settings` existente (mutación campo a campo) para
    que las referencias ya importadas (`from .config import settings`)
    vean los cambios sin reiniciar.

    ``env_file`` permite apuntar a otro fichero (la UI lo usa para poder
    testearse contra un ``.env`` temporal); vacío = no leer ningún fichero.

    Se valida de nuevo con pydantic: si el ``.env`` nuevo no parsea, el
    objeto live se conserva intacto y se propaga el error (el caller
    revierte el backup y responde 500). No deja nunca al proceso con una
    configuración a medias.
    """
    new = Settings(_env_file=env_file or None)
    live = settings
    for name in type(new).model_fields:
        setattr(live, name, getattr(new, name))
    return live

