"""Parámetros de generación estilo OpenAI y su política de propagación.

Política del proxy:

- **Respuesta final** (síntesis, o la ejecución atómica cuando el fast path la
  entrega directamente al cliente): se propagan tal cual los parámetros de
  muestreo del cliente — ``temperature``, ``top_p``, ``max_tokens``,
  ``max_completion_tokens``, ``stop``, ``seed``, ``presence_penalty``,
  ``frequency_penalty``, ``parallel_tool_calls`` — y ``response_format`` solo
  cuando la llamada no lleva herramientas (combinar JSON mode con tool calling
  es frágil entre proveedores).
- **Llamadas internas** (planificación y ejecución de tareas atómicas): se usa
  la temperatura interna configurada (determinista para planificar), ``seed``
  si el cliente lo pidió y ``parallel_tool_calls`` cuando la llamada ofrece
  herramientas. No se propagan ``stop``, ``response_format`` ni los límites de
  tokens del cliente, que recortarían el trabajo interno y no la respuesta
  final.
- ``stream_options`` no se reenvía al upstream (gobierna nuestro SSE): se
  respeta ``include_usage`` para decidir si emitimos el chunk final de uso.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from .runtime import RuntimeConfig


@dataclass
class GenerationParams:
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    stop: Optional[Union[str, List[str]]] = None
    seed: Optional[int] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    parallel_tool_calls: Optional[bool] = None
    response_format: Optional[Dict[str, Any]] = None
    stream_include_usage: bool = False

    # ------------------------------------------------------------------
    # Construcción
    # ------------------------------------------------------------------

    @classmethod
    def from_request(cls, request: Any) -> "GenerationParams":
        stream_options = getattr(request, "stream_options", None) or {}
        return cls(
            temperature=getattr(request, "temperature", None),
            top_p=getattr(request, "top_p", None),
            max_tokens=getattr(request, "max_tokens", None),
            max_completion_tokens=getattr(request, "max_completion_tokens", None),
            stop=getattr(request, "stop", None),
            seed=getattr(request, "seed", None),
            presence_penalty=getattr(request, "presence_penalty", None),
            frequency_penalty=getattr(request, "frequency_penalty", None),
            parallel_tool_calls=getattr(request, "parallel_tool_calls", None),
            response_format=getattr(request, "response_format", None),
            stream_include_usage=bool(stream_options.get("include_usage")),
        )

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "GenerationParams":
        if not data:
            return cls()
        known = set(cls.__dataclass_fields__)
        payload = {k: v for k, v in data.items() if k in known}
        if payload.get("stop") is not None and not isinstance(payload["stop"], (str, list)):
            payload["stop"] = None
        return cls(**payload)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "max_completion_tokens": self.max_completion_tokens,
            "stop": self.stop,
            "seed": self.seed,
            "presence_penalty": self.presence_penalty,
            "frequency_penalty": self.frequency_penalty,
            "parallel_tool_calls": self.parallel_tool_calls,
            "response_format": self.response_format,
            "stream_include_usage": self.stream_include_usage,
        }

    # ------------------------------------------------------------------
    # Payloads
    # ------------------------------------------------------------------

    def final_payload(self, *, with_tools: bool) -> Dict[str, Any]:
        """Parámetros para la llamada que produce la respuesta visible."""
        payload: Dict[str, Any] = {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "max_completion_tokens": self.max_completion_tokens,
            "stop": self.stop,
            "seed": self.seed,
            "presence_penalty": self.presence_penalty,
            "frequency_penalty": self.frequency_penalty,
        }
        if with_tools and self.parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = self.parallel_tool_calls
        if not with_tools and self.response_format is not None:
            payload["response_format"] = self.response_format
        return {k: v for k, v in payload.items() if v is not None}

    def internal_payload(
        self,
        config: RuntimeConfig,
        *,
        phase: str,
        with_tools: bool = False,
    ) -> Dict[str, Any]:
        """Parámetros para una llamada interna (planificar o ejecutar una tarea)."""
        temperature = config.planner.temperature if phase == "planner" else config.executor.temperature
        payload: Dict[str, Any] = {
            "temperature": temperature,
            "seed": self.seed,
            "max_tokens": config.internal_max_tokens,
        }
        if with_tools and self.parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = self.parallel_tool_calls
        return {k: v for k, v in payload.items() if v is not None}
