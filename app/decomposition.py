"""Validación fuerte de la descomposición devuelta por el modelo.

La respuesta de la Fase 1 se valida con modelos Pydantic antes de tocar el
árbol de tareas: formato, descripciones no vacías, cantidad máxima de
subtareas, identificadores únicos, dependencias existentes y ausencia de
ciclos. Si algo falla, el motor reintenta una única vez pidiendo la corrección
(``DecompositionError`` describe exactamente qué está mal); si vuelve a fallar,
la tarea se trata como atómica y el incidente queda registrado.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from pydantic import BaseModel, Field, ValidationError

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_ID_SANITIZE = re.compile(r"[^\w.\-]+")


class DecompositionError(Exception):
    """La descomposición no cumple el contrato esperado."""


class SubtaskSpec(BaseModel):
    id: str
    description: str
    depends_on: List[str] = Field(default_factory=list)


class DecompositionPlan(BaseModel):
    atomic: bool = True
    subtasks: List[SubtaskSpec] = Field(default_factory=list)
    # True solo si todas las subtareas declararon dependencias explícitamente
    # (sin eso, el motor no se fía del grafo para paralelizar).
    declares_dependencies: bool = False
    raw: str = ""


def _load_json(raw: str) -> Dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise DecompositionError("la respuesta del planificador llegó vacía")
    candidates = [text]
    match = _JSON_BLOCK.search(text)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"atomic": False, "subtasks": data}
    raise DecompositionError("la respuesta del planificador no es un objeto JSON válido")


def load_json_object(raw: str) -> Dict[str, Any]:
    """Carga un objeto JSON de una respuesta del modelo (tolerante a texto extra).

    Público para reutilizarlo fases que también esperan JSON (verificación).
    """
    return _load_json(raw)


def _clean_id(value: Any, fallback: str) -> str:
    text = _ID_SANITIZE.sub("_", str(value or "").strip())
    return text or fallback


def _normalize_subtask(item: Any, index: int) -> Dict[str, Any]:
    fallback_id = f"task_{index + 1}"
    if isinstance(item, str):
        return {"id": fallback_id, "description": item, "depends_on": [], "declared": False}
    if isinstance(item, dict):
        description = item.get("description")
        if description is None:
            description = item.get("task") or item.get("text") or item.get("title") or ""
        depends = item.get("depends_on")
        declared = "depends_on" in item or "dependencies" in item
        if depends is None:
            depends = item.get("dependencies") or []
        if isinstance(depends, (str, int)):
            depends = [depends]
        if not isinstance(depends, list):
            raise DecompositionError(
                f"la subtarea {index + 1} tiene 'depends_on' con un formato inválido"
            )
        return {
            "id": _clean_id(item.get("id"), fallback_id),
            "description": str(description),
            "depends_on": [str(dep) for dep in depends],
            "declared": declared,
        }
    raise DecompositionError(f"la subtarea {index + 1} no es un texto ni un objeto")


def _assert_acyclic(specs: List[SubtaskSpec]) -> None:
    known = {spec.id for spec in specs}
    graph = {spec.id: [dep for dep in spec.depends_on if dep in known] for spec in specs}
    visiting: set = set()
    done: set = set()

    def visit(node: str, path: List[str]) -> None:
        if node in done:
            return
        if node in visiting:
            cycle = " -> ".join(path + [node])
            raise DecompositionError(f"las dependencias forman un ciclo: {cycle}")
        visiting.add(node)
        for dep in graph.get(node, []):
            visit(dep, path + [node])
        visiting.discard(node)
        done.add(node)

    for spec in specs:
        visit(spec.id, [])


def parse_decomposition(raw: str, *, max_subtasks: int) -> DecompositionPlan:
    """Convierte la respuesta cruda del planificador en un plan validado."""
    data = _load_json(raw)

    raw_subtasks = data.get("subtasks")
    if raw_subtasks is None:
        raw_subtasks = []
    if not isinstance(raw_subtasks, list):
        raise DecompositionError("'subtasks' no es una lista")

    atomic_flag = data.get("atomic")
    if atomic_flag is None:
        atomic_flag = not raw_subtasks
    if not isinstance(atomic_flag, bool):
        raise DecompositionError("'atomic' no es un booleano")

    if atomic_flag:
        # Contradicción tolerada: "atomic" manda (mismo criterio que antes).
        return DecompositionPlan(atomic=True, subtasks=[], raw=raw)

    if not raw_subtasks:
        raise DecompositionError("'atomic' es false pero 'subtasks' está vacío")

    if len(raw_subtasks) > max_subtasks:
        raise DecompositionError(
            f"se pidieron {len(raw_subtasks)} subtareas y el máximo por nodo es {max_subtasks}"
        )

    normalized = [_normalize_subtask(item, index) for index, item in enumerate(raw_subtasks)]

    try:
        specs = [
            SubtaskSpec(
                id=item["id"],
                description=item["description"],
                depends_on=item["depends_on"],
            )
            for item in normalized
        ]
    except ValidationError as exc:  # pragma: no cover - defensivo
        raise DecompositionError(f"subtarea con formato inválido: {exc}") from exc

    for index, spec in enumerate(specs):
        if not spec.description.strip():
            raise DecompositionError(f"la subtarea {index + 1} tiene una descripción vacía")
        if spec.id in spec.depends_on:
            raise DecompositionError(f"la subtarea '{spec.id}' se declara dependiente de sí misma")

    ids = [spec.id for spec in specs]
    duplicates = {item for item in ids if ids.count(item) > 1}
    if duplicates:
        raise DecompositionError(f"identificadores de subtarea duplicados: {sorted(duplicates)}")

    known = set(ids)
    for spec in specs:
        unknown = [dep for dep in spec.depends_on if dep not in known]
        if unknown:
            raise DecompositionError(
                f"la subtarea '{spec.id}' depende de identificadores inexistentes: {unknown}"
            )

    _assert_acyclic(specs)

    declares = all(item["declared"] for item in normalized)
    return DecompositionPlan(
        atomic=False, subtasks=specs, declares_dependencies=declares, raw=raw
    )

