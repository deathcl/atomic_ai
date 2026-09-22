"""Configuración efectiva de una ejecución concreta.

``Settings`` describe el proceso completo; ``RuntimeConfig`` describe *esta*
petición: qué modelo y proveedor usa cada fase, con qué límites de presupuesto
y con qué perfil. Se resuelve una vez por request (aplicando el perfil pedido
por header) y se serializa en la sesión para que una reanudación continúe con
exactamente los mismos ajustes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

from .config import Settings, settings as global_settings
from .profiles import PROFILES, normalize_profile


@dataclass
class PhaseConfig:
    """Proveedor + modelo de una fase concreta."""

    model: str
    base_url: str = ""
    api_key: str = ""
    temperature: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PhaseConfig":
        return cls(
            model=str(data.get("model") or ""),
            base_url=str(data.get("base_url") or ""),
            api_key=str(data.get("api_key") or ""),
            temperature=data.get("temperature"),
        )


@dataclass
class RuntimeConfig:
    profile: str = "balanced"
    planner: PhaseConfig = field(default_factory=lambda: PhaseConfig(model=""))
    executor: PhaseConfig = field(default_factory=lambda: PhaseConfig(model=""))
    synthesis: PhaseConfig = field(default_factory=lambda: PhaseConfig(model=""))
    verification: PhaseConfig = field(default_factory=lambda: PhaseConfig(model=""))
    max_decomposition_depth: int = 3
    max_subtasks_per_node: int = 6
    max_total_tasks: int = 25
    max_upstream_calls: int = 40
    max_execution_seconds: float = 300.0
    max_accumulated_context_chars: int = 300_000
    max_previous_results_chars: int = 50_000
    fast_path: bool = False
    always_synthesize: bool = False
    enable_verification: bool = False
    verification_max_revisions: int = 1
    enable_parallel_tasks: bool = False
    max_parallel_tasks: int = 4
    trace_mode: str = "summary"
    multimodal_forwarding: str = "always"
    internal_max_tokens: Optional[int] = None
    model_prices: Dict[str, Dict[str, float]] = field(default_factory=dict)
    expose_metrics: bool = True

    def reserved_calls(self) -> int:
        """Llamadas que las tareas atómicas no pueden consumir.

        La ejecución siempre debe poder cerrar: síntesis (o verificación), y
        con verificación habilitada, la revisión adicional permitida.
        """
        reserved = 0 if (self.fast_path and not self.always_synthesize) else 1
        if self.enable_verification:
            reserved += 1 + max(0, self.verification_max_revisions)
        return reserved

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "RuntimeConfig":
        if not data:
            return cls()
        payload = dict(data)
        for key in ("planner", "executor", "synthesis", "verification"):
            payload[key] = PhaseConfig.from_dict(payload.get(key) or {})
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in payload.items() if k in known})


def _phase_config(
    base: Settings,
    requested_model: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
    temperature: Optional[float],
) -> PhaseConfig:
    return PhaseConfig(
        model=model or requested_model,
        base_url=(base_url or base.upstream_base_url).rstrip("/"),
        api_key=api_key or base.resolved_api_key(),
        temperature=temperature,
    )


def resolve_runtime(
    requested_model: str,
    *,
    profile: Optional[str] = None,
    base: Optional[Settings] = None,
) -> RuntimeConfig:
    """Combina `.env` + perfil + modelo pedido en una configuración de ejecución."""
    base = base or global_settings
    profile_name = normalize_profile(profile or base.atomic_profile)
    preset = PROFILES[profile_name]

    requested_model = requested_model or base.upstream_model
    # El perfil "quality" puede apuntar a un modelo más capaz; los overrides
    # explícitos por fase (PLANNER_MODEL, ...) siempre tienen prioridad.
    preferred = base.quality_model if (preset.use_quality_model and base.quality_model) else ""

    config = RuntimeConfig(
        profile=profile_name,
        planner=_phase_config(
            base,
            requested_model,
            model=base.planner_model or preferred,
            base_url=base.planner_base_url,
            api_key=base.planner_api_key,
            temperature=base.planner_temperature,
        ),
        executor=_phase_config(
            base,
            requested_model,
            model=base.executor_model or preferred,
            base_url=base.executor_base_url,
            api_key=base.executor_api_key,
            temperature=base.executor_temperature,
        ),
        synthesis=_phase_config(
            base,
            requested_model,
            model=base.synthesis_model or preferred,
            base_url=base.synthesis_base_url,
            api_key=base.synthesis_api_key,
            temperature=None,
        ),
        verification=_phase_config(
            base,
            requested_model,
            model=base.verification_model or preferred,
            base_url=base.verification_base_url,
            api_key=base.verification_api_key,
            temperature=0.0,
        ),
        max_decomposition_depth=base.max_decomposition_depth,
        max_subtasks_per_node=base.max_subtasks_per_node,
        max_total_tasks=base.max_total_tasks,
        max_upstream_calls=base.max_upstream_calls,
        max_execution_seconds=base.max_execution_seconds,
        max_accumulated_context_chars=base.max_accumulated_context_chars,
        max_previous_results_chars=base.max_previous_results_chars,
        fast_path=base.atomic_fast_path,
        always_synthesize=base.always_synthesize,
        enable_verification=base.enable_verification,
        verification_max_revisions=base.verification_max_revisions,
        enable_parallel_tasks=base.enable_parallel_tasks,
        max_parallel_tasks=base.max_parallel_tasks,
        trace_mode=base.effective_trace_mode(),
        multimodal_forwarding=base.multimodal_forwarding,
        internal_max_tokens=base.internal_max_tokens,
        model_prices={k: dict(v) for k, v in (base.model_prices or {}).items()},
        expose_metrics=base.expose_metrics,
    )

    _apply_preset(config, preset)
    return config


def _apply_preset(config: RuntimeConfig, preset) -> None:
    """El perfil pisa solo los ajustes que declara explícitamente."""
    overrides = {
        "max_decomposition_depth": preset.max_decomposition_depth,
        "max_subtasks_per_node": preset.max_subtasks_per_node,
        "max_total_tasks": preset.max_total_tasks,
        "max_upstream_calls": preset.max_upstream_calls,
        "max_execution_seconds": preset.max_execution_seconds,
        "max_previous_results_chars": preset.max_previous_results_chars,
        "fast_path": preset.fast_path,
        "always_synthesize": preset.always_synthesize,
        "enable_verification": preset.enable_verification,
        "enable_parallel_tasks": preset.enable_parallel_tasks,
        "trace_mode": preset.trace_mode,
    }
    for name, value in overrides.items():
        if value is not None:
            setattr(config, name, value)

