"""API de la UI (docs/UI_IMPLEMENTACION.md §6).

Reglas del proyecto que este módulo respeta:

* **Ninguna regla de validación a mano**: el ``PUT`` valida con
  ``ConfigUpdate`` (generado del catálogo en ``app/web/schemas.py``) y las
  claves que acepta ``/config/reset`` son exactamente las del catálogo.
* **Nada de lógica del motor duplicada**: el playground reutiliza
  ``_resolve_run`` de ``app/main.py`` y los helpers de ``app/sse.py``.
* **Escritura del `.env` solo vía ``app/web/envfile.py``** (backup + atómica)
  y con recarga en caliente de ``Settings``; si la configuración nueva no
  parsea, se revierte el fichero y se responde ``500`` (§7, Paso 4).

``app.main`` se importa *dentro* de los handlers (no en la cabecera) porque
``app.main`` monta este router: un import a nivel de módulo sería circular.

``ENV_PATH`` es la ruta del ``.env`` que edita la UI; los tests la apuntan a
un directorio temporal con ``monkeypatch.setattr(routes, "ENV_PATH", …)``.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterable, List, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import sse
from ..config import reload_settings, settings
from ..observability import log
from ..schemas import ChatCompletionRequest, ChatMessage
from ..upstream import UpstreamClient, UpstreamError
from . import catalog, envfile
from .catalog import ParamSpec
from .registry import registry
from .schemas import ConfigUpdate, env_updates

router = APIRouter(prefix="/ui/api", tags=["ui"])

ENV_PATH: Path = Path(".env")

MASK_PREFIX = "\u2022\u2022\u2022\u2022"          # ••••  (4 bullets)
NO_SECRET_TEXT = "(no definida)"
STATS_INTERVAL_SECONDS = 2.0        # tick del dashboard (§6)
STATS_MIN_INTERVAL_SECONDS = 0.25   # cota inferior del tick
STATS_MAX_INTERVAL_SECONDS = 60.0   # cota superior del tick
MAX_SESSIONS_IN_PAYLOAD = 20        # el panel no necesita 200 filas


# ------------------------------------------------------------------
# Seguridad (§6: X-UI-Token en PUT/DELETE/POST)
# ------------------------------------------------------------------

def require_token(x_ui_token: Optional[str] = Header(default=None, alias="X-UI-Token")) -> None:
    """Exige el header ``X-UI-Token`` si ``UI_TOKEN`` está definido.

    Vacío (default) = desactivado: aceptable porque el proxy escucha en
    ``127.0.0.1``; obligatorio si se expone en red.
    """
    expected = settings.ui_token
    if not expected:
        return
    if not x_ui_token or not secrets.compare_digest(x_ui_token, expected):
        raise HTTPException(status_code=401, detail="header X-UI-Token inválido o ausente")


# ------------------------------------------------------------------
# Catálogo (GET /catalog) y configuración (GET/PUT/POST /config)
# ------------------------------------------------------------------

def _json_default(value: Any) -> Any:
    """Default del catálogo → JSON (solo tipos planos; ``{}`` para precios)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return dict(value)
    return str(value)


def _spec_payload(spec: ParamSpec) -> Dict[str, Any]:
    """Un parámetro del catálogo tal cual lo consume el JS del formulario."""
    return {
        "key": spec.key,
        "field": spec.field,
        "group": spec.group,
        "label": spec.label,
        "description": spec.description,
        "widget": spec.widget,
        "options": list(spec.options),
        "minimum": spec.minimum,
        "maximum": spec.maximum,
        "pattern": spec.pattern,
        "max_length": spec.max_length,
        "secret": spec.secret,
        "restart_required": spec.restart_required,
        "placeholder": spec.placeholder,
        "default": _json_default(spec.default),
    }


def _mask_secret(value: Any) -> str:
    """Secreto → ``••••1234`` (4 últimos) o ``(no definida)``. Nunca en claro."""
    text = "" if value is None else str(value)
    if not text:
        return NO_SECRET_TEXT
    return f"{MASK_PREFIX}{text[-4:]}"


def _field_payload(spec: ParamSpec, env_values: Dict[str, str]) -> Dict[str, Any]:
    payload = _spec_payload(spec)
    raw = getattr(settings, spec.field)
    payload["value"] = _mask_secret(raw) if spec.secret else _json_default(raw)
    # is_set distingue "definido en el .env" de "usando el default del código":
    # sin esto la UI no puede mostrar bien el tri-estado de EXPOSE_REASONING_CONTENT.
    payload["is_set"] = spec.key in env_values
    return payload


def _config_payload() -> Dict[str, Any]:
    """Config efectiva agrupada por pestaña, con secrets enmascarados."""
    env_values = envfile.read_env(ENV_PATH)
    groups = [
        {
            "name": group,
            "fields": [
                _field_payload(spec, env_values)
                for spec in catalog.PARAMS
                if spec.group == group
            ],
        }
        for group in catalog.GROUPS
    ]
    return {
        "env_path": str(ENV_PATH),
        "groups": groups,
        "effective": {
            "profile": settings.atomic_profile,
            "trace_mode": settings.effective_trace_mode(),
            "model": settings.upstream_model,
        },
        "secrets_are_write_only": True,
    }


@router.get("/catalog")
async def get_catalog() -> Dict[str, Any]:
    """El catálogo completo: el JS del formulario genera los widgets desde aquí."""
    return {
        "groups": list(catalog.GROUPS),
        "widgets": list(catalog.WIDGETS),
        "params": [_spec_payload(spec) for spec in catalog.PARAMS],
    }


@router.get("/config")
async def get_config() -> Dict[str, Any]:
    return _config_payload()


def _is_mask(value: str) -> bool:
    text = (value or "").strip()
    return text == NO_SECRET_TEXT or text.startswith(MASK_PREFIX)


def _drop_unchanged_secrets(updates: Dict[str, str]) -> Dict[str, str]:
    """Un secret que llega vacío o enmascarado NO se reescribe (§5.1).

    Para *borrar* un secreto (o devolverlo a su default) está
    ``POST /config/reset``, que sí escribe el default del catálogo.
    """
    secret_keys = {spec.key for spec in catalog.PARAMS if spec.secret}
    return {
        key: value
        for key, value in updates.items()
        if not (key in secret_keys and (value == "" or _is_mask(value)))
    }


def _restart_keys(keys: Iterable[str]) -> List[str]:
    """De las claves tocadas, las que exigen reiniciar el proceso."""
    by_key = {spec.key: spec for spec in catalog.PARAMS}
    return [key for key in keys if by_key[key].restart_required]


def _original_env() -> Optional[str]:
    return ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else None


def _apply_and_reload(updates: Dict[str, str]) -> List[str]:
    """``write_env`` + ``reload_settings`` con rollback del fichero.

    Nunca deja al proceso con una configuración a medias: o queda aplicada y
    recargada, o el ``.env`` vuelve byte a byte a su estado previo.
    """
    original = _original_env()
    try:
        envfile.write_env(ENV_PATH, updates)
    except (envfile.EnvFileError, RuntimeError) as exc:
        raise HTTPException(
            status_code=500, detail=f"no se pudo escribir el .env: {exc}"
        ) from exc

    try:
        reload_settings(str(ENV_PATH))
    except Exception as exc:  # .env nuevo no parsea → rollback y 500
        envfile.restore(ENV_PATH, original)
        try:
            reload_settings(str(ENV_PATH))
        except Exception:  # pragma: no cover - el .env previo ya estaba cargado
            pass
        log("ui_config_rejected", level=30, error=str(exc), rolled_back=True)
        raise HTTPException(
            status_code=500, detail=f"configuración inválida; .env revertido: {exc}"
        ) from exc

    restart_required = _restart_keys(updates)
    log("ui_config_updated", keys=sorted(updates), restart_required=restart_required)
    return restart_required


@router.put("/config", dependencies=[Depends(require_token)])
async def put_config(update: ConfigUpdate) -> Dict[str, Any]:
    """Valida (422) → backup → reescribe solo lo enviado → recarga en caliente.

    Un campo ausente/``null`` no se toca; un valor fuera de opciones, rangos o
    patrón del catálogo lo rechaza ``ConfigUpdate`` antes de llegar aquí.
    """
    updates = _drop_unchanged_secrets(env_updates(update))
    if not updates:
        payload = _config_payload()
        payload.update({"applied": False, "changed": [], "restart_required": []})
        return payload

    restart_required = _apply_and_reload(updates)
    payload = _config_payload()
    payload.update(
        {
            "applied": True,
            "changed": sorted(updates),
            "restart_required": restart_required,
        }
    )
    return payload


class ResetRequest(BaseModel):
    """``{"fields": ["ATOMIC_PROFILE"]}`` — claves ENV del catálogo."""

    fields: List[str] = Field(min_length=1)


@router.post("/config/reset", dependencies=[Depends(require_token)])
async def reset_config(body: ResetRequest) -> Dict[str, Any]:
    """Restaura el default del catálogo (los de default ``None`` se borran)."""
    by_key = {spec.key: spec for spec in catalog.PARAMS}
    unknown = sorted({key for key in body.fields if key not in by_key})
    if unknown:
        raise HTTPException(status_code=422, detail=f"claves fuera del catálogo: {unknown}")

    fields = list(dict.fromkeys(body.fields))  # sin duplicados, orden estable
    original = _original_env()
    try:
        envfile.reset_fields(ENV_PATH, fields, catalog)
    except (envfile.EnvFileError, RuntimeError) as exc:
        raise HTTPException(
            status_code=500, detail=f"no se pudo escribir el .env: {exc}"
        ) from exc

    try:
        reload_settings(str(ENV_PATH))
    except Exception as exc:
        envfile.restore(ENV_PATH, original)
        raise HTTPException(
            status_code=500, detail=f"configuración inválida; .env revertido: {exc}"
        ) from exc

    payload = _config_payload()
    payload.update(
        {
            "applied": True,
            "changed": fields,
            "restart_required": _restart_keys(fields),
        }
    )
    return payload


# ------------------------------------------------------------------
# Dashboard (GET /stats + stream) y sesiones (§6)
# ------------------------------------------------------------------

def _session_payload(state: Any) -> Dict[str, Any]:
    """Resumen de una sesión para el panel (sin volcar el árbol completo)."""
    goal = getattr(state, "goal_ctx", None)
    return {
        "session_id": state.session_id,
        "model": state.model,
        "age_seconds": round(max(0.0, time.time() - state.last_used_at), 1),
        "paused": bool(state.pending_phase),
        "pending_phase": state.pending_phase,
        "pending_leaf_index": state.pending_leaf_index,
        "pending_tool_calls": len(state.pending_tool_calls),
        "results": len(state.results),
        "tasks": len(state.leaves),
        "turns": len(state.turn_history),
        "goal": (goal.turn_instruction or "")[:160] if goal is not None else "",
    }


def _sessions_summary() -> Dict[str, Any]:
    from ..main import session_store  # import local: evita el ciclo main ↔ routes

    states = session_store.snapshot()
    return {
        "backend": settings.session_backend,
        "ttl_seconds": settings.session_ttl_seconds,
        "max_sessions": settings.max_sessions,
        "active": len(states),
        "paused": sum(1 for state in states if state.pending_phase),
        "sessions": [_session_payload(state) for state in states[:MAX_SESSIONS_IN_PAYLOAD]],
    }


def _stats_payload() -> Dict[str, Any]:
    snapshot = registry.snapshot()
    snapshot["config"] = {
        "profile": settings.atomic_profile,
        "trace_mode": settings.effective_trace_mode(),
        "model": settings.upstream_model,
        "expose_metrics": settings.expose_metrics,
    }
    snapshot["sessions"] = _sessions_summary()
    return snapshot


@router.get("/stats")
async def get_stats() -> Dict[str, Any]:
    """Agregados del registro en memoria + estado de sesiones + config efectiva."""
    return _stats_payload()


async def stats_events(interval: float = STATS_INTERVAL_SECONDS) -> AsyncIterator[str]:
    """Generador SSE del dashboard: un tick cada ``interval`` segundos.

    Separado de la ruta para poder testearlo sin cliente HTTP
    (``anext(routes.stats_events())``) y para que el tick sea parametrizable.
    """
    while True:
        payload = json.dumps(_stats_payload(), ensure_ascii=False, default=str)
        yield f"event: stats\ndata: {payload}\n\n"
        await asyncio.sleep(interval)


@router.get("/stats/stream")
async def stats_stream(interval: float = STATS_INTERVAL_SECONDS) -> StreamingResponse:
    # Cota dura del tick: el intervalo no es un parámetro del .env, pero
    # tampoco puede permitir que un cliente martillee el endpoint.
    tick = min(max(float(interval), STATS_MIN_INTERVAL_SECONDS), STATS_MAX_INTERVAL_SECONDS)
    return StreamingResponse(stats_events(tick), media_type="text/event-stream")


@router.get("/sessions")
async def list_sessions() -> Dict[str, Any]:
    return _sessions_summary()


@router.delete("/sessions/{session_id}", dependencies=[Depends(require_token)])
async def delete_session(session_id: str) -> Dict[str, Any]:
    from ..main import session_store

    if not await session_store.delete(session_id):
        raise HTTPException(status_code=404, detail="sesión no encontrada")
    return {"deleted": session_id}


# ------------------------------------------------------------------
# Playground (POST /playground): el motor real, en streaming (§6)
# ------------------------------------------------------------------

class PlaygroundRequest(BaseModel):
    """Cuerpo del playground: mensajes + perfil (select cerrado)."""

    messages: List[ChatMessage]
    model: Optional[str] = None
    profile: Optional[Literal["fast", "balanced", "quality"]] = None
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    max_tokens: Optional[int] = Field(default=None, ge=1, le=1_000_000)


def _named_event(name: str, block: str) -> str:
    """Etiqueta un bloque SSE ya formateado por ``app/sse.py``.

    El payload sigue siendo exactamente el chunk OpenAI-compatible de
    ``/v1/chat/completions``: el playground no inventa formato nuevo, solo
    nombra cada chunk para que el JS sepa qué pintar donde corresponda.
    """
    return f"event: {name}\n{block}"


def _plain_event(name: str, payload: Dict[str, Any]) -> str:
    """Evento SSE cuyo ``data`` es un JSON propio de la UI (metrics, error)."""
    data = json.dumps(payload, ensure_ascii=False, default=str)
    return f"event: {name}\ndata: {data}\n\n"


async def _playground_stream(prepared: Any) -> AsyncIterator[str]:
    """Convierte los eventos del motor en SSE etiquetado (sin duplicar nada)."""
    model = prepared.model
    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    yield _named_event("chunk", sse.role_chunk(model, chunk_id))

    try:
        async for kind, payload in prepared.events:
            if kind == "reasoning":
                if settings.effective_trace_mode() == "off":
                    continue  # mismo criterio que /v1/chat/completions
                yield _named_event("reasoning", sse.reasoning_chunk(model, payload, chunk_id))
            elif kind == "content":
                yield _named_event("content", sse.content_chunk(model, payload, chunk_id))
            elif kind == "tool_calls":
                yield _named_event(
                    "tool_calls", sse.raw_delta_chunk(model, {"tool_calls": payload}, chunk_id)
                )
                yield _named_event(
                    "chunk", sse.final_chunk(model, chunk_id, finish_reason="tool_calls")
                )
                break
        else:
            yield _named_event("chunk", sse.final_chunk(model, chunk_id))
    except UpstreamError as exc:
        yield _plain_event("error", {"message": str(exc), "type": "upstream_error"})
        yield _named_event("done", sse.done())
        return

    metrics = prepared.engine.metrics.to_dict(prepared.engine.config.model_prices)
    yield _plain_event("metrics", metrics)
    yield _named_event("done", sse.done())


@router.post("/playground", dependencies=[Depends(require_token)])
async def playground(body: PlaygroundRequest) -> StreamingResponse:
    """Reproduce el flujo real (descomposición → ejecución → síntesis) en SSE.

    Reutiliza ``_resolve_run`` — el mismo camino que ``/v1/chat/completions`` —
    y **no persiste sesión**: el playground es un banco de pruebas y no debe
    contaminar las sesiones de los clientes reales.
    """
    from ..main import _resolve_run

    requested_model = body.model or settings.upstream_model
    request = ChatCompletionRequest(
        model=requested_model,
        messages=body.messages,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
    )
    prepared = await _resolve_run(request, UpstreamClient(), requested_model, profile=body.profile)
    return StreamingResponse(_playground_stream(prepared), media_type="text/event-stream")



