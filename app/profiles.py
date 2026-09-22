"""Perfiles de ejecución listos para usar (fast / balanced / quality).

Un perfil es un conjunto de ajustes que se aplican por encima de la
configuración base (`.env`) para una petición concreta, sin tocar el archivo.
Se puede forzar por request con el header ``X-Atomic-Profile``; si no se manda,
manda ``ATOMIC_PROFILE``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from .observability import log

PROFILE_NAMES: Tuple[str, ...] = ("fast", "balanced", "quality")


@dataclass(frozen=True)
class ProfilePreset:
    """Ajustes de un perfil. ``None`` = no pisa la configuración base."""

    max_decomposition_depth: Optional[int] = None
    max_subtasks_per_node: Optional[int] = None
    max_total_tasks: Optional[int] = None
    max_upstream_calls: Optional[int] = None
    max_execution_seconds: Optional[float] = None
    max_previous_results_chars: Optional[int] = None
    fast_path: Optional[bool] = None
    always_synthesize: Optional[bool] = None
    enable_verification: Optional[bool] = None
    enable_parallel_tasks: Optional[bool] = None
    trace_mode: Optional[str] = None
    use_quality_model: bool = False


PROFILES = {
    # Barato y directo: poca profundidad, fast path y sin verificación.
    "fast": ProfilePreset(
        max_decomposition_depth=1,
        max_subtasks_per_node=3,
        max_total_tasks=6,
        max_upstream_calls=8,
        max_execution_seconds=120.0,
        max_previous_results_chars=20_000,
        fast_path=True,
        always_synthesize=False,
        enable_verification=False,
        trace_mode="summary",
    ),
    # Punto medio: usa la configuración base tal cual (límites moderados,
    # síntesis normal, sin forzar nada).
    "balanced": ProfilePreset(),
    # Máxima calidad: más profundidad, modelo más capaz y verificación final.
    "quality": ProfilePreset(
        max_decomposition_depth=4,
        max_subtasks_per_node=8,
        max_total_tasks=40,
        max_upstream_calls=60,
        max_execution_seconds=600.0,
        max_previous_results_chars=120_000,
        enable_verification=True,
        trace_mode="full",
        use_quality_model=True,
    ),
}


def normalize_profile(name: str | None) -> str:
    """Devuelve un perfil válido o ``"balanced"`` si el nombre no se reconoce."""
    candidate = (name or "").strip().lower()
    if candidate in PROFILES:
        return candidate
    if candidate:
        log(
            "profile_unknown",
            level=30,
            requested=candidate,
            fallback="balanced",
            valid=list(PROFILE_NAMES),
        )
    return "balanced"
