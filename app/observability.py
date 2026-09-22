"""Observabilidad del proxy: logs estructurados y métricas por ejecución.

Cada request atendida por el proxy recibe un ``request_id`` y acumula un
``RunMetrics`` con llamadas al upstream, reintentos, duración por fase, tokens
y coste estimado. Los logs se emiten en JSON (una línea por evento) para que
puedan consumirse sin parseo frágil desde cualquier colector.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

LOGGER_NAME = "atomic_ai"
logger = logging.getLogger(LOGGER_NAME)

# Precio en USD por millón de tokens: {"modelo": {"prompt": 0.27, "completion": 1.1}}
_PRICE_KEYS_PER_MILLION = {
    "prompt": ("prompt", "input"),
    "completion": ("completion", "output"),
}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
            + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "event": record.getMessage(),
        }
        payload.update(getattr(record, "fields", None) or {})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    """Configura el logger del proxy una sola vez (idempotente)."""
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    if any(getattr(handler, "_atomic_ai_handler", False) for handler in logger.handlers):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    handler._atomic_ai_handler = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    # No duplicamos eventos hacia el logger raíz de uvicorn.
    logger.propagate = False


def log(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Emite un evento estructurado (JSON) con el request_id que corresponda."""
    logger.log(level, event, extra={"fields": fields})


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:16]}"


@dataclass
class Usage:
    """Tokens agregados de una o varias llamadas al upstream."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, other: Optional["Usage"]) -> "Usage":
        if other is None:
            return self
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        return self

    def __bool__(self) -> bool:
        return bool(self.prompt_tokens or self.completion_tokens or self.total_tokens)

    def to_dict(self) -> Dict[str, int]:
        total = self.total_tokens or (self.prompt_tokens + self.completion_tokens)
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": total,
        }

    @classmethod
    def from_upstream(cls, data: Any) -> Optional["Usage"]:
        """Normaliza el bloque ``usage`` de un proveedor OpenAI-compatible."""
        if not isinstance(data, dict):
            return None
        prompt = data.get("prompt_tokens", data.get("input_tokens"))
        completion = data.get("completion_tokens", data.get("output_tokens"))
        total = data.get("total_tokens")
        if prompt is None and completion is None and total is None:
            return None
        prompt = int(prompt or 0)
        completion = int(completion or 0)
        total = int(total) if total is not None else prompt + completion
        return cls(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)

    @classmethod
    def from_dict(cls, data: Any) -> "Usage":
        if not isinstance(data, dict):
            return cls()
        return cls(
            prompt_tokens=int(data.get("prompt_tokens") or 0),
            completion_tokens=int(data.get("completion_tokens") or 0),
            total_tokens=int(data.get("total_tokens") or 0),
        )


@dataclass
class PhaseMetrics:
    phase: str
    calls: int = 0
    retries: int = 0
    duration_seconds: float = 0.0
    usage: Usage = field(default_factory=Usage)
    models: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phase": self.phase,
            "calls": self.calls,
            "retries": self.retries,
            "duration_seconds": round(self.duration_seconds, 3),
            "usage": self.usage.to_dict(),
            "models": list(self.models),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PhaseMetrics":
        return cls(
            phase=str(data.get("phase") or "unknown"),
            calls=int(data.get("calls") or 0),
            retries=int(data.get("retries") or 0),
            duration_seconds=float(data.get("duration_seconds") or 0.0),
            usage=Usage.from_dict(data.get("usage")),
            models=[str(m) for m in (data.get("models") or [])],
        )


def estimate_cost(
    prices: Optional[Dict[str, Dict[str, float]]],
    usage_by_model: Dict[str, Usage],
) -> Optional[float]:
    """Coste estimado en USD a partir de precios por millón de tokens.

    Si no hay precios configurados para ningún modelo usado, devuelve ``None``
    (no se inventa un coste).
    """
    if not prices or not usage_by_model:
        return None
    total = 0.0
    priced = False
    for model, usage in usage_by_model.items():
        price = prices.get(model)
        if not price:
            continue
        prompt_price = None
        completion_price = None
        for key in _PRICE_KEYS_PER_MILLION["prompt"]:
            if price.get(key) is not None:
                prompt_price = float(price[key])
                break
        for key in _PRICE_KEYS_PER_MILLION["completion"]:
            if price.get(key) is not None:
                completion_price = float(price[key])
                break
        if prompt_price is None and completion_price is None:
            continue
        priced = True
        total += (usage.prompt_tokens / 1_000_000) * (prompt_price or 0.0)
        total += (usage.completion_tokens / 1_000_000) * (completion_price or 0.0)
    if not priced:
        return None
    return round(total, 6)


@dataclass
class RunMetrics:
    """Métricas agregadas de una ejecución completa del proxy."""

    request_id: str = field(default_factory=new_request_id)
    profile: str = "balanced"
    subtasks: int = 0
    tool_calls: int = 0
    errors: List[str] = field(default_factory=list)
    budget_notes: List[str] = field(default_factory=list)
    limits_hit: List[str] = field(default_factory=list)
    phases: Dict[str, PhaseMetrics] = field(default_factory=dict)
    usage_by_model: Dict[str, Usage] = field(default_factory=dict)
    started_at: float = field(default_factory=time.perf_counter)

    def phase(self, name: str) -> PhaseMetrics:
        metrics = self.phases.get(name)
        if metrics is None:
            metrics = PhaseMetrics(phase=name)
            self.phases[name] = metrics
        return metrics

    def record_call(self, phase: str, model: str) -> None:
        metrics = self.phase(phase)
        metrics.calls += 1
        if model and model not in metrics.models:
            metrics.models.append(model)

    def record_retry(self, phase: str, reason: str = "") -> None:
        self.phase(phase).retries += 1

    def record_usage(self, phase: str, model: str, usage: Optional[Usage]) -> None:
        if not usage:
            return
        self.phase(phase).usage.add(usage)
        if model:
            self.usage_by_model.setdefault(model, Usage()).add(usage)

    def record_duration(self, phase: str, seconds: float) -> None:
        self.phase(phase).duration_seconds += max(0.0, seconds)

    def record_tool_calls(self, count: int) -> None:
        self.tool_calls += count

    def record_error(self, message: str) -> None:
        self.errors.append(message)

    def note_limit(self, limit: str, message: str) -> str:
        if limit and limit not in self.limits_hit:
            self.limits_hit.append(limit)
        if message not in self.budget_notes:
            self.budget_notes.append(message)
        return message

    def total_duration(self) -> float:
        return max(0.0, time.perf_counter() - self.started_at)

    @property
    def total_usage(self) -> Usage:
        total = Usage()
        for usage in self.usage_by_model.values():
            total.add(usage)
        if not total:
            for metrics in self.phases.values():
                total.add(metrics.usage)
        return total

    def to_dict(self, prices: Optional[Dict[str, Dict[str, float]]] = None) -> Dict[str, Any]:
        usage = self.total_usage
        return {
            "request_id": self.request_id,
            "profile": self.profile,
            "subtasks": self.subtasks,
            "tool_calls": self.tool_calls,
            "upstream_calls": sum(m.calls for m in self.phases.values()),
            "retries": sum(m.retries for m in self.phases.values()),
            "duration_seconds": round(self.total_duration(), 3),
            "phase_durations": {
                name: round(m.duration_seconds, 3) for name, m in self.phases.items()
            },
            "usage": usage.to_dict(),
            "usage_by_model": {
                model: item.to_dict() for model, item in self.usage_by_model.items()
            },
            "phases": {name: m.to_dict() for name, m in self.phases.items()},
            "cost_usd": estimate_cost(prices, self.usage_by_model),
            "limits_hit": list(self.limits_hit),
            "notes": list(self.budget_notes),
            "errors": list(self.errors),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "RunMetrics":
        if not data:
            return cls()
        metrics = cls(
            request_id=str(data.get("request_id") or new_request_id()),
            profile=str(data.get("profile") or "balanced"),
            subtasks=int(data.get("subtasks") or 0),
            tool_calls=int(data.get("tool_calls") or 0),
            errors=[str(e) for e in (data.get("errors") or [])],
            budget_notes=[str(n) for n in (data.get("notes") or [])],
            limits_hit=[str(l) for l in (data.get("limits_hit") or [])],
        )
        for name, payload in (data.get("phases") or {}).items():
            metrics.phases[name] = PhaseMetrics.from_dict(dict(payload, phase=name))
        for model, usage in (data.get("usage_by_model") or {}).items():
            metrics.usage_by_model[model] = Usage.from_dict(usage)
        return metrics

