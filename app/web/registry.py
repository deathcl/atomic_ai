"""Registro en memoria de las últimas ejecuciones (dashboard de la UI, §6).

``RunMetrics`` vive lo que dura una petición; el dashboard necesita algo que
sobreviva entre peticiones. Este módulo guarda un resumen de las últimas
``MAX_RUNS`` ejecuciones más los agregados acumulados, sin tocar disco y sin
saber nada del motor:

* ``app/main.py`` llama a ``registry.record(...)`` al terminar cada run
  (streaming o no);
* ``app/web/routes.py`` lo lee para ``GET /ui/api/stats`` y su stream.

Si el proxy se reinicia el histórico empieza de cero: la persistencia del
histórico es Fase 2 (§8 del documento de la UI), no Fase 1.
"""
from __future__ import annotations

import time
from collections import Counter, deque
from typing import Any, Deque, Dict, List, Optional

MAX_RUNS = 50
RECENT_IN_SNAPSHOT = 10


class RunRegistry:
    """Agregados + últimas ejecuciones, todo en memoria."""

    def __init__(self, max_runs: int = MAX_RUNS, started_at: Optional[float] = None) -> None:
        self._runs: Deque[Dict[str, Any]] = deque(maxlen=max_runs)
        self._started_at = started_at if started_at is not None else time.time()
        self._totals: Dict[str, float] = self._empty_totals()
        self._limits: Counter = Counter()
        self._phase_calls: Counter = Counter()
        self._phase_retries: Counter = Counter()
        self._errors = 0

    @staticmethod
    def _empty_totals() -> Dict[str, float]:
        return {
            "runs": 0,
            "upstream_calls": 0,
            "retries": 0,
            "tool_calls": 0,
            "subtasks": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "duration_seconds": 0.0,
        }

    def record(self, summary: Dict[str, Any]) -> None:
        """Acumula el resumen de un run: ``RunMetrics.to_dict(prices)``."""
        usage = summary.get("usage") or {}
        totals = self._totals
        totals["runs"] += 1
        totals["upstream_calls"] += int(summary.get("upstream_calls") or 0)
        totals["retries"] += int(summary.get("retries") or 0)
        totals["tool_calls"] += int(summary.get("tool_calls") or 0)
        totals["subtasks"] += int(summary.get("subtasks") or 0)
        totals["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        totals["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        totals["total_tokens"] += int(usage.get("total_tokens") or 0)
        totals["cost_usd"] = round(float(totals["cost_usd"]) + float(summary.get("cost_usd") or 0.0), 6)
        totals["duration_seconds"] = round(
            float(totals["duration_seconds"]) + float(summary.get("duration_seconds") or 0.0), 3
        )

        for limit in summary.get("limits_hit") or []:
            self._limits[str(limit)] += 1
        for name, phase in (summary.get("phases") or {}).items():
            self._phase_calls[name] += int(phase.get("calls") or 0)
            self._phase_retries[name] += int(phase.get("retries") or 0)
        self._errors += len(summary.get("errors") or [])
        self._runs.append(dict(summary))

    def reset(self) -> None:
        """Borra el histórico y los agregados (útil en tests y al arrancar)."""
        self._runs.clear()
        self._totals = self._empty_totals()
        self._limits.clear()
        self._phase_calls.clear()
        self._phase_retries.clear()
        self._errors = 0

    def snapshot(self, recent: int = RECENT_IN_SNAPSHOT) -> Dict[str, Any]:
        """Uptime, agregados, límites alcanzados y las últimas ejecuciones."""
        entries: List[Dict[str, Any]] = list(self._runs)[-recent:]
        names = sorted(set(self._phase_calls) | set(self._phase_retries))
        return {
            "uptime_seconds": round(max(0.0, time.time() - self._started_at), 1),
            "max_runs": self._runs.maxlen,
            "runs_recorded": len(self._runs),
            "totals": dict(self._totals),
            "errors": self._errors,
            "by_phase": {
                name: {"calls": self._phase_calls[name], "retries": self._phase_retries[name]}
                for name in names
            },
            "limits_hit": dict(self._limits),
            "recent": entries[::-1],  # el más reciente primero
        }


registry = RunRegistry()
