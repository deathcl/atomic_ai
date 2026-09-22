"""Presupuesto global de ejecución.

Los límites existen para que un plan excesivo, un modelo que se atasca o un
upstream lento no se conviertan en coste o latencia descontrolados. El motor
consulta este objeto antes de planificar, antes de ejecutar cada tarea atómica
y antes de cada llamada al upstream; cuando un límite se alcanza, deja de
crear trabajo nuevo, sintetiza con lo disponible y anota la limitación para
que la respuesta final pueda explicarla.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ExecutionBudget:
    max_subtasks_per_node: int = 6
    max_total_tasks: int = 25
    max_upstream_calls: int = 40
    max_execution_seconds: float = 300.0
    max_accumulated_context_chars: int = 300_000
    # Llamadas que quedan reservadas para las fases de cierre (síntesis y,
    # si está habilitada, verificación): las tareas atómicas nunca consumen
    # ese colchón, de modo que una ejecución siempre puede terminar de
    # responder con lo que tenga.
    reserved_calls: int = 1
    started_at: float = field(default_factory=time.time)
    tasks_created: int = 0
    tasks_started: int = 0
    upstream_calls: int = 0
    accumulated_chars: int = 0
    notes: List[str] = field(default_factory=list)
    limits_hit: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Consultas
    # ------------------------------------------------------------------

    @property
    def deadline(self) -> float:
        return self.started_at + self.max_execution_seconds

    def seconds_left(self) -> float:
        return max(0.0, self.deadline - time.time())

    def time_exhausted(self) -> bool:
        return self.seconds_left() <= 0.0

    def remaining_new_tasks(self) -> int:
        return max(0, self.max_total_tasks - self.tasks_created)

    def calls_left(self, reserve: int = 0) -> int:
        return max(0, self.max_upstream_calls - self.upstream_calls - reserve)

    def can_spend_call(self, reserve: int = 0) -> bool:
        return self.calls_left(reserve) > 0

    def remaining_context_chars(self) -> int:
        return max(0, self.max_accumulated_context_chars - self.accumulated_chars)

    def can_start_task(self) -> bool:
        """¿Se puede arrancar una tarea atómica más (llamada + tiempo + tareas)?"""
        if self.tasks_started >= self.max_total_tasks:
            return False
        if not self.can_spend_call(reserve=self.reserved_calls):
            return False
        return not self.time_exhausted()

    # ------------------------------------------------------------------
    # Registro de consumo
    # ------------------------------------------------------------------

    def register_call(self) -> None:
        self.upstream_calls += 1

    def register_tasks_created(self, count: int) -> None:
        self.tasks_created += max(0, count)

    def register_task_started(self) -> None:
        self.tasks_started += 1

    def register_context_chars(self, count: int) -> None:
        self.accumulated_chars += max(0, count)

    def add_note(self, limit: str, message: str) -> str:
        if limit and limit not in self.limits_hit:
            self.limits_hit.append(limit)
        if message not in self.notes:
            self.notes.append(message)
        return message

    # ------------------------------------------------------------------
    # Serialización (para reanudar una ejecución pausada por tool calls)
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_subtasks_per_node": self.max_subtasks_per_node,
            "max_total_tasks": self.max_total_tasks,
            "max_upstream_calls": self.max_upstream_calls,
            "max_execution_seconds": self.max_execution_seconds,
            "max_accumulated_context_chars": self.max_accumulated_context_chars,
            "reserved_calls": self.reserved_calls,
            "started_at": self.started_at,
            "tasks_created": self.tasks_created,
            "tasks_started": self.tasks_started,
            "upstream_calls": self.upstream_calls,
            "accumulated_chars": self.accumulated_chars,
            "notes": list(self.notes),
            "limits_hit": list(self.limits_hit),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "ExecutionBudget":
        if not data:
            return cls()
        return cls(
            max_subtasks_per_node=int(data.get("max_subtasks_per_node") or 6),
            max_total_tasks=int(data.get("max_total_tasks") or 25),
            max_upstream_calls=int(data.get("max_upstream_calls") or 40),
            max_execution_seconds=float(data.get("max_execution_seconds") or 300.0),
            max_accumulated_context_chars=int(data.get("max_accumulated_context_chars") or 300_000),
            reserved_calls=int(data.get("reserved_calls") or 0),
            started_at=float(data.get("started_at") or time.time()),
            tasks_created=int(data.get("tasks_created") or 0),
            tasks_started=int(data.get("tasks_started") or 0),
            upstream_calls=int(data.get("upstream_calls") or 0),
            accumulated_chars=int(data.get("accumulated_chars") or 0),
            notes=[str(n) for n in (data.get("notes") or [])],
            limits_hit=[str(l) for l in (data.get("limits_hit") or [])],
        )
