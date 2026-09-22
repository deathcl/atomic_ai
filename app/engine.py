"""Motor de descomposición atómica.

Orquesta las fases del proxy (planificar → ejecutar → sintetizar → verificar),
aplicando el presupuesto global de ejecución, la validación fuerte del plan, el
grafo de dependencias (con paralelización segura), el fast path para tareas
atómicas, la compresión del contexto y los modos de trazabilidad.

Los eventos que emite el motor son tuplas ``(tipo, payload)``:

- ``("reasoning", texto)``: progreso resumido de las fases (lo que se muestra
  con ``TRACE_MODE=summary``).
- ``("reasoning_detail", texto)``: detalle interno (trabajo de cada tarea,
  árbol de subtareas); solo con ``TRACE_MODE=full``.
- ``("content", texto)``: respuesta visible para el cliente.
- ``("tool_calls", lista)``: pausa del protocolo OpenAI esperando el resultado
  de una herramienta.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Set, Tuple

from . import prompts
from .budget import ExecutionBudget
from .config import settings
from .content import build_multimodal_content
from .context import render_results
from .decomposition import DecompositionError, load_json_object, parse_decomposition
from .observability import RunMetrics, log
from .params import GenerationParams
from .runtime import PhaseConfig, RuntimeConfig
from .tools import has_side_effect_tools
from .upstream import UpstreamClient, UpstreamError

Event = tuple[str, Any]
Phase = Literal["leaf", "synthesis"]

# Marcadores internos que nunca llegan al cliente.
PENDING = "_tool_calls_pending"
DONE = "_phase_done"
INTERNAL_KINDS = frozenset({PENDING, DONE})

# Palabras que, con MULTIMODAL_FORWARDING=smart, marcan una fase como
# relevante para adjuntar las imágenes de la conversación.
IMAGE_KEYWORDS = (
    "imagen",
    "imágenes",
    "imagenes",
    "foto",
    "captura",
    "pantallazo",
    "screenshot",
    "diagrama",
    "gráfico",
    "grafico",
    "gráfica",
    "adjunto",
    "adjunta",
    "ocr",
    "visual",
)


class MissingToolOutputError(Exception):
    """Una reanudación llegó sin alguno de los resultados de tool pendientes.

    Nunca se sustituye un resultado faltante por una cadena vacía: sería
    inventar información que el modelo tomaría como real.
    """


def _merge_tool_call_delta(acc: dict[int, dict], deltas: list[dict]) -> None:
    for d in deltas:
        idx = d.get("index", 0)
        entry = acc.setdefault(
            idx, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}}
        )
        if d.get("id"):
            entry["id"] = d["id"]
        if d.get("type"):
            entry["type"] = d["type"]
        fn = d.get("function") or {}
        if fn.get("name"):
            entry["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            entry["function"]["arguments"] += fn["arguments"]


@dataclass
class TaskNode:
    description: str
    depth: int
    node_id: str = ""
    raw_id: str = ""
    # node_id (cualificado) de las hermanas de las que depende esta tarea.
    depends_on: List[str] = field(default_factory=list)
    children: List["TaskNode"] = field(default_factory=list)
    is_atomic: bool = False
    result: Optional[str] = None
    skipped: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "description": self.description,
            "depth": self.depth,
            "node_id": self.node_id,
            "raw_id": self.raw_id,
            "depends_on": list(self.depends_on),
            "children": [child.to_dict() for child in self.children],
            "is_atomic": self.is_atomic,
            "result": self.result,
            "skipped": self.skipped,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskNode":
        node = cls(
            description=str(data.get("description") or ""),
            depth=int(data.get("depth") or 0),
            node_id=str(data.get("node_id") or ""),
            raw_id=str(data.get("raw_id") or ""),
            depends_on=[str(dep) for dep in (data.get("depends_on") or [])],
            is_atomic=bool(data.get("is_atomic")),
            result=data.get("result"),
            skipped=bool(data.get("skipped")),
        )
        node.children = [cls.from_dict(child) for child in (data.get("children") or [])]
        return node


@dataclass
class GoalContext:
    """Resultado de separar una request entrante en sus partes con distinto
    rol: el system prompt del caller (autoridad real, no dato a clasificar),
    la instrucción del turno actual (lo único que se decompone), el contexto
    de turnos previos (fondo, nunca redecompuesto) y las partes de imagen
    encontradas en cualquier mensaje."""

    caller_system: str
    turn_instruction: str
    prior_context: str
    image_parts: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "caller_system": self.caller_system,
            "turn_instruction": self.turn_instruction,
            "prior_context": self.prior_context,
            "image_parts": list(self.image_parts),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "GoalContext":
        data = data or {}
        return cls(
            caller_system=str(data.get("caller_system") or ""),
            turn_instruction=str(data.get("turn_instruction") or ""),
            prior_context=str(data.get("prior_context") or ""),
            image_parts=list(data.get("image_parts") or []),
        )


def _collect_atomic_leaves(node: TaskNode) -> List[TaskNode]:
    if node.is_atomic:
        return [node]
    leaves: List[TaskNode] = []
    for child in node.children:
        leaves.extend(_collect_atomic_leaves(child))
    return leaves


def _render_tree(node: TaskNode, depth: int = 0) -> List[str]:
    lines: List[str] = []
    for child in node.children:
        marker = " (atómica)" if child.is_atomic else ""
        label = f"{child.node_id}: {child.description}" if child.node_id else child.description
        lines.append("  " * depth + f"- {label}{marker}")
        lines.extend(_render_tree(child, depth + 1))
    return lines


def _qualify(parent: TaskNode, raw_id: str) -> str:
    return f"{parent.node_id}.{raw_id}" if parent.node_id else raw_id


def _short(text: str, limit: int = 90) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _chunk_text(text: str, size: int = 240) -> List[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


@dataclass
class _LeafOutcome:
    index: int
    events: List[Event] = field(default_factory=list)
    text: str = ""
    pending: Optional[Dict[str, Any]] = None


class AtomicDecompositionEngine:
    def __init__(
        self,
        client: UpstreamClient,
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Any = None,
        *,
        config: Optional[RuntimeConfig] = None,
        budget: Optional[ExecutionBudget] = None,
        metrics: Optional[RunMetrics] = None,
        params: Optional[GenerationParams] = None,
    ) -> None:
        self._client = client
        self._model = model
        if config is None:
            config = RuntimeConfig(
                planner=PhaseConfig(model=model),
                executor=PhaseConfig(model=model),
                synthesis=PhaseConfig(model=model),
                verification=PhaseConfig(model=model),
                max_decomposition_depth=settings.max_decomposition_depth,
                max_subtasks_per_node=settings.max_subtasks_per_node,
                max_total_tasks=settings.max_total_tasks,
                max_upstream_calls=settings.max_upstream_calls,
                max_execution_seconds=settings.max_execution_seconds,
                max_accumulated_context_chars=settings.max_accumulated_context_chars,
                max_previous_results_chars=settings.max_previous_results_chars,
                fast_path=settings.atomic_fast_path,
                always_synthesize=settings.always_synthesize,
                enable_verification=settings.enable_verification,
                verification_max_revisions=settings.verification_max_revisions,
                enable_parallel_tasks=settings.enable_parallel_tasks,
                max_parallel_tasks=settings.max_parallel_tasks,
                trace_mode=settings.effective_trace_mode(),
                multimodal_forwarding=settings.multimodal_forwarding,
                internal_max_tokens=settings.internal_max_tokens,
                model_prices={k: dict(v) for k, v in (settings.model_prices or {}).items()},
                expose_metrics=settings.expose_metrics,
            )
        self.config = config
        self.budget = budget or ExecutionBudget(
            max_subtasks_per_node=config.max_subtasks_per_node,
            max_total_tasks=config.max_total_tasks,
            max_upstream_calls=config.max_upstream_calls,
            max_execution_seconds=config.max_execution_seconds,
            max_accumulated_context_chars=config.max_accumulated_context_chars,
            reserved_calls=config.reserved_calls(),
        )
        self.metrics = metrics or RunMetrics(profile=config.profile)
        self.params = params or GenerationParams()
        self._tools = tools
        self._tool_choice = tool_choice

        # goal_ctx se asigna una vez por turno externo (ver app/main.py), antes
        # de llamar a run()/resume() — es invariante durante todo ese run.
        self.goal_ctx: Optional[GoalContext] = None

        # Estado del árbol de tareas y de la ejecución, expuesto como atributos
        # públicos para que una sesión persistida pueda restaurarlo entre
        # peticiones HTTP distintas (ver app/session.py).
        self.root: Optional[TaskNode] = None
        self.leaves: List[TaskNode] = []
        self.results: List[str] = []
        self.result_entries: List[Tuple[str, str]] = []
        self._completed: Set[int] = set()
        self._leaf_prereqs: Dict[int, Set[int]] = {}
        self._parallel_active = False
        self._deps_declared = True

        # Estado de pausa/reanudación, unificado entre hoja atómica y síntesis
        # final (ambas pueden disparar tool calls y ambas deben poder
        # reanudarse con el historial completo de rondas ya vistas).
        self.pending_phase: Optional[Phase] = None
        self.pending_leaf_index: Optional[int] = None
        self.pending_tool_calls: List[Dict[str, Any]] = []
        self.pending_conversation: List[Dict[str, Any]] = []
        self.tool_round_count: int = 0

        # Fast path: la ejecución atómica se entrega como respuesta final.
        self.fast_path_active = False

        # Identificadores de tool calls ya resueltos: evita reejecutar o
        # reaceptar resultados de herramientas ya consumidos.
        self.resolved_tool_call_ids: List[str] = []

        # Resultados internos de la fase de síntesis con verificación.
        self._last_pending_payload: Optional[Dict[str, Any]] = None
        self._last_synthesis_text = ""
        self._last_final_answer = ""
        self._last_revision_text = ""


    # ------------------------------------------------------------------
    # Soporte: prompts de verificación y helpers internos
    # ------------------------------------------------------------------

    VERIFICATION_SYSTEM_PROMPT = (
        "<rol>\n"
        "Eres un verificador de respuestas. Recibes el objetivo original y una "
        "respuesta candidata, y decides si la respuesta resuelve el objetivo de "
        "forma completa, correcta y coherente.\n"
        "</rol>\n\n"
        "<reglas>\n"
        "- Responde ÚNICAMENTE con un objeto JSON con esta forma exacta:\n"
        '  {"ok": true, "revised": ""} si la respuesta es válida.\n'
        '  {"ok": false, "revised": "<respuesta corregida completa>"} si no lo es.\n'
        "- Si corriges, entrega la respuesta completa corregida, no un diff.\n"
        "- No cambies el estilo ni añadas comentarios sobre este proceso.\n"
        "</reglas>\n"
    )

    VERIFICATION_USER_PROMPT = (
        "<objetivo>\n{goal}\n</objetivo>\n\n"
        "<respuesta_candidata>\n{answer}\n</respuesta_candidata>\n\n"
        "Verifica la respuesta y responde con el JSON indicado."
    )

    def _ensure_providers(self) -> None:
        """Publica la configuración por fase en el cliente upstream (si sabe de
        providers). Con un solo modelo configurado es equivalente al default."""
        setter = getattr(self._client, "set_providers", None)
        if setter is None:
            return
        try:
            setter(
                {
                    "planner": self.config.planner,
                    "executor": self.config.executor,
                    "synthesis": self.config.synthesis,
                    "verification": self.config.verification,
                }
            )
        except Exception:  # pragma: no cover - cliente alternativo en tests
            pass

    def _phase_model(self, phase: str) -> str:
        config = getattr(self.config, phase, None)
        model = getattr(config, "model", "") if config is not None else ""
        return model or self._model

    def _compose_system(self, base_prompt: str) -> str:
        from . import prompts

        caller = (self.goal_ctx.caller_system if self.goal_ctx else "") or ""
        if caller.strip():
            preamble = prompts.CALLER_SYSTEM_PREAMBLE.format(caller_system=caller)
            return f"{preamble}\n\n{base_prompt}"
        return base_prompt

    def _tools_text(self) -> str:
        if not self._tools:
            return "(ninguna herramienta disponible)"
        lines = []
        for tool in self._tools:
            function = tool.get("function") or {}
            name = function.get("name") or "?"
            description = function.get("description") or "(sin descripción)"
            lines.append(f"{name}: {description}")
        return "\n".join(lines)

    def _multimodal_user(self, text: str) -> Any:
        """Content del mensaje user: texto plano, o lista con imágenes si el
        turno trajo imágenes y el modo de reenvío multimodal lo permite."""
        if self.config.multimodal_forwarding == "off":
            return text
        images = list(self.goal_ctx.image_parts) if self.goal_ctx else []
        if not images:
            return text
        from .content import build_multimodal_content

        return build_multimodal_content(text, images)

    def _render_previous_results(self) -> str:
        """Resultados ya calculados de este turno, condensados al presupuesto
        de contexto configurado (los más recientes primero al recortar)."""
        from .context import condense_result

        entries = self.result_entries or [
            (f"tarea_{i + 1}", text) for i, text in enumerate(self.results)
        ]
        if not entries:
            return "(sin resultados)"
        max_chars = max(500, int(self.config.max_previous_results_chars))
        per = max(400, max_chars // len(entries))
        lines: List[str] = []
        total = 0
        for node_id, text in entries:
            share = max_chars - total
            if share <= 0:
                lines.append("[…resultados previos omitidos por límite de contexto…]")
                break
            label = node_id or "tarea"
            condensed = condense_result(text, min(per, share), label=label)
            lines.append(f"[{label}] {condensed}")
            total += len(condensed)
        return "\n\n".join(lines)

    def _call_allowed(self) -> bool:
        try:
            return self.budget.can_spend_call(reserve=self.config.reserved_calls())
        except Exception:  # pragma: no cover - defensivo
            return True

    def _max_tool_rounds(self) -> int:
        return max(0, int(settings.max_tool_rounds_per_phase))


    # ------------------------------------------------------------------
    # Helpers de estado de hoja y registro de resultados
    # ------------------------------------------------------------------

    def _leaf_state(self, index: int) -> Dict[str, Any]:
        return self._leaf_states.setdefault(index, {})

    def _record_result(self, text: str) -> None:
        self.results.append(text)
        self._last_result_chars = len(text or "")
        self._last_result_skipped = False
        if self.config.max_previous_results_chars > 0 and self.budget is not None:
            used = sum(len(r or "") for r in self.results)
            try:
                self.budget.register_context_chars(used)
            except Exception:  # pragma: no cover - defensivo
                pass

    # ------------------------------------------------------------------
    # Trazabilidad, telemetría y contenido de los mensajes user
    # ------------------------------------------------------------------

    def _progress_events(self, text: str) -> List[Event]:
        """Narración resumida del run (visible con TRACE_MODE=summary o full)."""
        if self.config.trace_mode in ("summary", "full"):
            return [("reasoning", text)]
        return []

    def _detail_events(self, text: str) -> List[Event]:
        """Detalle interno del run (solo visible con TRACE_MODE=full)."""
        if self.config.trace_mode == "full" and text:
            return [("reasoning", text)]
        return []

    def _bind_client_telemetry(self) -> None:
        """Conecta el presupuesto y las métricas del run con el cliente
        upstream: cada llamada (y cada reintento) queda registrada en el
        mismo presupuesto y en las mismas métricas que gobierna el motor."""
        try:
            self._client._budget = self.budget  # type: ignore[attr-defined]
            self._client._metrics = self.metrics  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - cliente alternativo en tests
            pass

    def _user_content(self, text: str, phase: str = "planner") -> Any:
        """Content del mensaje user según el modo de reenvío multimodal:

        - ``always``: imágenes en todas las fases.
        - ``execution_only``: imágenes solo en la fase de ejecución atómica.
        - ``smart``: imágenes en la fase que mencione contenido visual.
        - ``off``: nunca (texto plano).
        """
        mode = self.config.multimodal_forwarding
        if mode == "off":
            return text
        images = list(self.goal_ctx.image_parts) if self.goal_ctx else []
        if not images:
            return text
        if mode == "execution_only" and phase != "executor":
            return text
        if mode == "smart":
            haystack = " ".join(
                part
                for part in (
                    self.goal_ctx.turn_instruction if self.goal_ctx else "",
                    text,
                )
                if part
            ).lower()
            if not any(keyword in haystack for keyword in IMAGE_KEYWORDS):
                return text
        return build_multimodal_content(text, images)

    # ------------------------------------------------------------------
    # Fase 1: descomposición del objetivo
    # ------------------------------------------------------------------

    def _decomposition_inputs(self, task: str) -> Tuple[str, str]:
        assert self.goal_ctx is not None
        system = self._compose_system(prompts.DECOMPOSITION_SYSTEM_PROMPT)
        user = prompts.DECOMPOSITION_USER_PROMPT.format(
            goal=self.goal_ctx.turn_instruction,
            prior_context=self.goal_ctx.prior_context or "(sin contexto previo)",
            tools=self._tools_text(),
            task=task,
        )
        return system, user

    def _degradation_note(self, limit: str, reason: str) -> str:
        note = self.budget.add_note(limit, reason)
        self.metrics.note_limit(limit, note)
        return note

    async def _plan_node(self, node: TaskNode) -> None:
        """Clasifica `node` con el planner y cuelga sus subtareas validadas.

        Nunca lanza por culpa del upstream: ante una respuesta ilegible o un
        presupuesto agotado, la tarea se degrada a atómica para que la
        ejecución siga siendo posible con lo que hay.
        """
        depth = node.depth + 1
        if depth > self.config.max_decomposition_depth:
            return
        if self.budget.time_exhausted():
            self._degradation_note(
                "tiempo",
                "presupuesto de tiempo agotado; el resto se trata como atómico",
            )
            node.is_atomic = True
            return
        reserve = self.config.reserved_calls()
        if self.budget.calls_left(reserve=reserve) <= 0:
            self._degradation_note(
                "llamadas",
                "sin llamadas upstream suficientes; el resto se trata como atómico",
            )
            node.is_atomic = True
            return
        if self.budget.remaining_new_tasks() <= 0:
            self._degradation_note(
                "tareas", "límite de tareas totales alcanzado; se trata como atómico"
            )
            node.is_atomic = True
            return

        system, user = self._decomposition_inputs(node.description)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": self._user_content(user, "planner")},
        ]
        started = time.perf_counter()
        try:
            message = await self._client.complete_raw(
                messages,
                model=self._phase_model("planner"),
                phase="planner",
                json_mode=True,
                params=self.params.internal_payload(self.config, phase="planner"),
            )
        finally:
            self.metrics.record_duration("planner", time.perf_counter() - started)
        raw = message.get("content") or ""
        max_new = min(
            self.config.max_subtasks_per_node, max(0, self.budget.remaining_new_tasks())
        )
        try:
            plan = parse_decomposition(raw, max_subtasks=max_new)
        except DecompositionError as exc:
            self.metrics.record_error(f"plan ilegible para '{_short(node.description)}': {exc}")
            node.is_atomic = True
            return
        if plan.declares_dependencies:
            self._deps_declared = True
        if plan.atomic or not plan.subtasks:
            node.is_atomic = True
            return
        self.budget.register_tasks_created(len(plan.subtasks))
        for position, spec in enumerate(plan.subtasks):
            raw_id = spec.id or str(position + 1)
            node.children.append(
                TaskNode(
                    description=spec.description,
                    depth=depth,
                    node_id=_qualify(node, raw_id),
                    raw_id=raw_id,
                    depends_on=[_qualify(node, dep) for dep in spec.depends_on if dep],
                )
            )

    def _compute_prereqs(self) -> None:
        """Resuelve las dependencias declaradas por el planner contra los ids
        cualificados de las hojas: prerequisito = índice de la hoja hermana."""
        index_by_id = {leaf.node_id: i for i, leaf in enumerate(self.leaves)}
        self._leaf_prereqs = {}
        for index, leaf in enumerate(self.leaves):
            prereqs = {
                index_by_id[dep]
                for dep in leaf.depends_on
                if dep in index_by_id and index_by_id[dep] != index
            }
            self._leaf_prereqs[index] = prereqs
        self._deps_declared = any(self._leaf_prereqs.values())

    def _recompute_fast_path(self) -> None:
        """Fast path: un único paso atómico se entrega como respuesta final
        sin fase de síntesis (salvo que always_synthesize o la verificación
        lo exijan)."""
        self.fast_path_active = bool(
            self.config.fast_path
            and len(self.leaves) == 1
            and not self.config.always_synthesize
            and not self.config.enable_verification
        )

    def _execution_order(self) -> List[int]:
        """Orden de ejecución de las hojas: natural si no hay dependencias
        declaradas; topológico estable (Kahn) si las hay."""
        order = list(range(len(self.leaves)))
        if not (self.config.enable_parallel_tasks and self._deps_declared):
            return order
        result: List[int] = []
        done: Set[int] = set()
        remaining = list(order)
        while remaining:
            ready = [
                i
                for i in remaining
                if all(p in done for p in (self._leaf_prereqs.get(i) or set()))
            ]
            if not ready:
                ready = [remaining[0]]  # dependencias imposibles: orden natural
            chosen = ready[0]
            result.append(chosen)
            done.add(chosen)
            remaining.remove(chosen)
        return result

    # ------------------------------------------------------------------
    # Fase 2: ejecución de las tareas atómicas
    # ------------------------------------------------------------------

    def _leaf_inputs(self, leaf: TaskNode) -> Tuple[str, str]:
        assert self.goal_ctx is not None
        system = self._compose_system(prompts.EXECUTE_ATOMIC_SYSTEM_PROMPT)
        user = prompts.EXECUTE_ATOMIC_USER_PROMPT.format(
            goal=self.goal_ctx.turn_instruction,
            prior_context=self.goal_ctx.prior_context or "(sin contexto previo)",
            context=self._render_previous_results(),
            task=leaf.description,
        )
        return system, user

    def _leaf_seed(self, index: int) -> List[Dict[str, Any]]:
        system, user = self._leaf_inputs(self.leaves[index])
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": self._user_content(user, "executor")},
        ]

    # ------------------------------------------------------------------
    # Pausa y reanudación por tool calls
    # ------------------------------------------------------------------

    def _enter_pending(
        self,
        phase: Phase,
        leaf_index: Optional[int],
        tool_calls: List[Dict[str, Any]],
        conversation: List[Dict[str, Any]],
    ) -> None:
        self.pending_phase = phase
        self.pending_leaf_index = leaf_index
        self.pending_tool_calls = [dict(tc) for tc in tool_calls]
        self.pending_conversation = [dict(m) for m in conversation]

    def _finish_pending(self, tool_outputs: Dict[str, str]) -> List[Dict[str, Any]]:
        """Cierra una pausa: valida que llegaron todos los resultados de tool
        pendientes (nunca se sustituyen por vacíos), los añade a la
        conversación en curso y resetea el estado de pausa."""
        pending_ids = [tc.get("id") or "" for tc in self.pending_tool_calls]
        missing = [tc_id for tc_id in pending_ids if tc_id and tc_id not in tool_outputs]
        if missing:
            raise MissingToolOutputError(
                "Reanudación sin resultados de herramienta para: " + ", ".join(missing)
            )
        assistant_msg = {
            "role": "assistant",
            "content": None,
            "tool_calls": [dict(tc) for tc in self.pending_tool_calls],
        }
        conversation = list(self.pending_conversation) + [assistant_msg]
        for tc in self.pending_tool_calls:
            tc_id = tc.get("id") or ""
            if tc_id in tool_outputs:
                conversation.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": (tc.get("function") or {}).get("name"),
                        "content": tool_outputs[tc_id],
                    }
                )
                if tc_id not in self.resolved_tool_call_ids:
                    self.resolved_tool_call_ids.append(tc_id)
        self.pending_phase = None
        self.pending_leaf_index = None
        self.pending_tool_calls = []
        self.pending_conversation = []
        return conversation

    # ------------------------------------------------------------------
    # Bucle central de fase (streaming + rondas de tool calls)
    # ------------------------------------------------------------------

    async def _run_phase(
        self,
        *,
        seed_messages: List[Dict[str, Any]],
        phase: str,
        leaf_index: Optional[int] = None,
        emit_content: bool = True,
    ) -> AsyncIterator[Event]:
        """Ejecuta una fase en streaming. Termina con DONE (texto final de la
        fase) o PENDING (tool calls esperando resultados del caller). El
        contenido de la ejecución de hojas nunca se emite: es material
        interno para la síntesis; solo la respuesta final llega como
        "content" al cliente."""
        messages = list(seed_messages)
        forced_rounds = 0
        # La pausa de una hoja se marca como "leaf" (contrato de sesión),
        # aunque la fase upstream se llame "executor".
        pause_phase: Phase = "leaf" if phase == "executor" else "synthesis"
        while True:
            tools = list(self._tools) if self._tools else None
            # Límite de rondas alcanzado: la siguiente llamada va sin tools
            # para forzar una respuesta de texto (ver test del límite).
            force_text = bool(tools) and self.tool_round_count >= self._max_tool_rounds()
            if force_text:
                tools = None
            tool_choice = "auto" if tools else None
            params = self.params.internal_payload(self.config, phase=phase, with_tools=bool(tools))
            acc: Dict[int, Dict[str, Any]] = {}
            content_parts: List[str] = []
            async for chunk in self._client.stream_raw(
                messages,
                model=self._phase_model(phase),
                phase=phase,
                json_mode=False,
                tools=tools,
                tool_choice=tool_choice,
                params=params,
            ):
                delta = chunk.get("delta") or {}
                piece = delta.get("content")
                if piece:
                    content_parts.append(piece)
                    if emit_content:
                        yield ("content", piece)
                reasoning_piece = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning_piece:
                    yield ("reasoning", reasoning_piece)
                if delta.get("tool_calls"):
                    _merge_tool_call_delta(acc, delta["tool_calls"])
                if chunk.get("finish_reason"):
                    break
            tool_calls = [acc[i] for i in sorted(acc)] if acc else []
            if tool_calls and tools and not force_text:
                normalized: List[Dict[str, Any]] = []
                for position, entry in enumerate(tool_calls):
                    entry = dict(entry)
                    if not entry.get("id"):
                        entry["id"] = f"call_{position + 1}"
                    normalized.append(entry)
                self.tool_round_count += 1
                self.metrics.record_tool_calls(len(normalized))
                messages = messages + [
                    {
                        "role": "assistant",
                        "content": "".join(content_parts) or None,
                        "tool_calls": [dict(tc) for tc in normalized],
                    }
                ]
                self._enter_pending(pause_phase, leaf_index, normalized, messages)
                yield (PENDING, {"tool_calls": [dict(tc) for tc in normalized]})
                return
            if tool_calls:
                # El modelo insiste en herramientas pese a pedírsele texto:
                # se reitera la instrucción sin aceptar la invocación.
                forced_rounds += 1
                if forced_rounds >= 3:
                    yield (DONE, "".join(content_parts))
                    return
                messages.append(
                    {
                        "role": "user",
                        "content": "(Responde únicamente con texto final; no invoques herramientas.)",
                    }
                )
                continue
            yield (DONE, "".join(content_parts))
            return

    async def _leaf_events(self, index: int, seed: List[Dict[str, Any]]) -> AsyncIterator[Event]:
        async for kind, payload in self._run_phase(
            seed_messages=seed, phase="executor", leaf_index=index, emit_content=False
        ):
            if kind == PENDING:
                yield ("tool_calls", payload["tool_calls"])
                return
            if kind == DONE:
                text = str(payload or "")
                leaf = self.leaves[index]
                leaf.result = text
                self._completed.add(index)
                self._record_result(text)
                self.result_entries.append((leaf.node_id or f"tarea_{index + 1}", text))
                return
            yield (kind, payload)

    async def _execute_leaf_step(self, index: int) -> AsyncIterator[Event]:
        """Ejecuta una hoja concreta aplicando: saltos por trabajo ya hecho,
        dependencias no resueltas, presupuesto de tiempo y de tareas."""
        leaf = self.leaves[index]
        if leaf.result is not None or index in self._completed or leaf.skipped:
            return
        prereqs = self._leaf_prereqs.get(index) or set()
        unmet = [
            p for p in sorted(prereqs) if self.leaves[p].result is None or self.leaves[p].skipped
        ]
        if unmet:
            leaf.skipped = True
            for event in self._progress_events(
                f"Tarea omitida por dependencias no resueltas: {_short(leaf.description)}\n"
            ):
                yield event
            return
        if self.budget.time_exhausted():
            leaf.skipped = True
            note = self._degradation_note(
                "tiempo", f"tiempo agotado; '{_short(leaf.description)}' omitida"
            )
            for event in self._progress_events(f"{note}\n"):
                yield event
            return
        if not self.budget.can_start_task() or not self._call_allowed():
            leaf.skipped = True
            note = self._degradation_note(
                "tareas",
                f"límite de tareas/llamadas alcanzado; '{_short(leaf.description)}' omitida",
            )
            for event in self._progress_events(f"{note}\n"):
                yield event
            return
        self.budget.register_task_started()
        for event in self._progress_events(f"Trabajando: {_short(leaf.description)}…\n"):
            yield event
        started = time.perf_counter()
        try:
            async for event in self._leaf_events(index, self._leaf_seed(index)):
                yield event
        finally:
            self.metrics.record_duration("executor", time.perf_counter() - started)

    async def execute_tree(self) -> AsyncIterator[Event]:
        """Fase 2: ejecuta todas las hojas atómicas en orden (topológico si
        hay dependencias declaradas y paralelización activada)."""
        order = self._execution_order()
        parallel = self.config.enable_parallel_tasks and self._deps_declared
        if not parallel:
            for index in order:
                async for event in self._execute_leaf_step(index):
                    yield event
                    if event[0] == "tool_calls":
                        return
            return
        wave_size = max(1, self.config.max_parallel_tasks)
        remaining = list(order)
        processed: Set[int] = set()
        while remaining:
            ready = [
                i
                for i in remaining
                if all(p in processed for p in (self._leaf_prereqs.get(i) or set()))
            ]
            if not ready:
                ready = [remaining[0]]
            wave = ready[:wave_size]
            outcomes = {i: _LeafOutcome(index=i) for i in wave}

            async def _run_one(outcome: _LeafOutcome) -> None:
                async for kind, payload in self._execute_leaf_step(outcome.index):
                    outcome.events.append((kind, payload))
                    if kind == "content":
                        outcome.text += payload
                    elif kind == PENDING:
                        outcome.pending = payload

            await asyncio.gather(*(_run_one(outcomes[i]) for i in wave))
            paused: Optional[_LeafOutcome] = None
            for i in wave:
                outcome = outcomes[i]
                for kind, payload in outcome.events:
                    yield (kind, payload)
                if outcome.pending is not None and paused is None:
                    paused = outcome
                processed.add(i)
                remaining.remove(i)
            if paused is not None:
                # Con varias hojas en pausa simultánea solo se propaga la
                # primera: es el caso raro; las demás se reejecutan al reanudar.
                return

    # ------------------------------------------------------------------
    # Fase 3: síntesis final (con verificación opcional)
    # ------------------------------------------------------------------

    def _synthesis_seed(self) -> List[Dict[str, Any]]:
        assert self.goal_ctx is not None
        system = self._compose_system(prompts.SYNTHESIS_SYSTEM_PROMPT)
        user = prompts.SYNTHESIS_USER_PROMPT.format(
            goal=self.goal_ctx.turn_instruction,
            prior_context=self.goal_ctx.prior_context or "(sin contexto previo)",
            context=self._render_previous_results(),
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": self._user_content(user, "synthesis")},
        ]

    async def synthesize_final(self) -> AsyncIterator[Event]:
        assert self.goal_ctx is not None
        self._ensure_providers()
        self.tool_round_count = 0
        self.pending_conversation = []
        async for event in self._finalize(
            self._run_phase(
                seed_messages=self._synthesis_seed(),
                phase="synthesis",
                emit_content=not self.config.enable_verification,
            )
        ):
            yield event

    async def _continue_synthesis(
        self, conversation: List[Dict[str, Any]]
    ) -> AsyncIterator[Event]:
        async for event in self._finalize(
            self._run_phase(
                seed_messages=conversation,
                phase="synthesis",
                emit_content=not self.config.enable_verification,
            )
        ):
            yield event

    async def _finalize(self, stream: AsyncIterator[Event]) -> AsyncIterator[Event]:
        """Consume una fase de síntesis: registra duración, guarda el texto y,
        si la verificación está activada, valida/corrige la respuesta antes de
        emitirla como contenido."""
        started = time.perf_counter()
        text = ""
        paused = False
        try:
            async for kind, payload in stream:
                if kind == PENDING:
                    paused = True
                    yield ("tool_calls", payload["tool_calls"])
                    return
                if kind == DONE:
                    text = str(payload or "")
                    continue
                yield (kind, payload)
        finally:
            self.metrics.record_duration("synthesis", time.perf_counter() - started)
        self._last_synthesis_text = text
        if paused:
            return
        if self.config.enable_verification:
            async for event in self._verify_and_maybe_revise(text):
                yield event
            return
        self._last_final_answer = text

    async def _verify_and_maybe_revise(self, answer: str) -> AsyncIterator[Event]:
        """Fase de verificación: comprueba la respuesta contra el objetivo y
        aplica hasta verification_max_revisions correcciones completas."""
        assert self.goal_ctx is not None
        current = answer
        attempts = max(1, int(self.config.verification_max_revisions))
        for attempt in range(attempts + 1):
            user = self.VERIFICATION_USER_PROMPT.format(
                goal=self.goal_ctx.turn_instruction, answer=current
            )
            messages = [
                {
                    "role": "system",
                    "content": self._compose_system(self.VERIFICATION_SYSTEM_PROMPT),
                },
                {"role": "user", "content": user},
            ]
            started = time.perf_counter()
            try:
                message = await self._client.complete_raw(
                    messages,
                    model=self._phase_model("verification"),
                    phase="verification",
                    json_mode=True,
                    params=self.params.internal_payload(self.config, phase="planner"),
                )
            finally:
                self.metrics.record_duration(
                    "verification", time.perf_counter() - started
                )
            try:
                verdict = load_json_object(message.get("content") or "")
            except DecompositionError as exc:
                self.metrics.record_error(f"verificación ilegible: {exc}")
                break
            if verdict.get("ok", True):
                for event in self._progress_events("Verificación: la respuesta es válida.\n"):
                    yield event
                break
            revised = str(verdict.get("revised") or "").strip()
            if not revised:
                break
            current = revised
            note = (
                "Verificación: se corrige la respuesta y se reevalúa.\n"
                if attempt < attempts
                else "Verificación: se aplica la corrección final.\n"
            )
            for event in self._progress_events(note):
                yield event
        self._last_final_answer = current or answer
        for piece in _chunk_text(current or answer, 240):
            yield ("content", piece)

    # ------------------------------------------------------------------
    # Orquestación de alto nivel
    # ------------------------------------------------------------------

    async def run(self) -> AsyncIterator[Event]:
        assert self.goal_ctx is not None
        self._ensure_providers()
        self._bind_client_telemetry()
        for event in self._progress_events(
            "Fase 1 de 3. Primero comienzo dividiendo la tarea en sus subtareas atómicas.\n\n"
        ):
            yield event
        async for event in self.build_task_tree():
            yield event
        for event in self._progress_events(
            "Listo, tenemos la lista completa del árbol de tareas hasta sus subtareas atómicas.\n\n"
        ):
            yield event
        tree_lines = _render_tree(self.root) if self.root else []
        if tree_lines:
            for event in self._detail_events("\n".join(tree_lines) + "\n\n"):
                yield event
        for event in self._progress_events(
            "Fase 2 de 3. Ahora ejecuto cada subtarea atómica en orden.\n\n"
        ):
            yield event
        tool_break = False
        async for event in self.execute_tree():
            if event[0] == "tool_calls":
                tool_break = True
            yield event
        if tool_break:
            return
        if self.fast_path_active:
            for event in self._progress_events(
                "Respuesta atómica directa (fast path); no se requiere síntesis.\n"
            ):
                yield event
            return
        for event in self._progress_events(
            "Fase 3 de 3. Listo, todas las tareas atómicas trabajadas correctamente, procedo a dar la respuesta final.\n"
        ):
            yield event
        async for event in self.synthesize_final():
            yield event

    async def _execute_remaining(self) -> AsyncIterator[Event]:
        """Continúa la fase 2 con las hojas que quedan sin resolver (tras una
        reanudación; las ya resueltas u omitidas se saltan solas)."""
        for index in self._execution_order():
            async for event in self._execute_leaf_step(index):
                yield event
                if event[0] == "tool_calls":
                    return

    async def resume_phase(self, tool_outputs: Dict[str, str]) -> AsyncIterator[Event]:
        """Continúa exactamente la fase que quedó pausada por la tool call."""
        resuming_phase = self.pending_phase
        leaf_index = self.pending_leaf_index
        conversation = self._finish_pending(tool_outputs)
        if resuming_phase == "leaf":
            index = leaf_index if leaf_index is not None else 0
            started = time.perf_counter()
            try:
                async for event in self._leaf_events(index, conversation):
                    yield event
            finally:
                self.metrics.record_duration("executor", time.perf_counter() - started)
        else:
            async for event in self._continue_synthesis(conversation):
                yield event

    async def resume(self, tool_outputs: Dict[str, str]) -> AsyncIterator[Event]:
        """Reanuda un run() previamente pausado por una tool call (en una hoja
        o en la síntesis), sin volver a descomponer el objetivo ni reejecutar
        trabajo ya resuelto."""
        assert self.goal_ctx is not None
        self._ensure_providers()
        self._bind_client_telemetry()
        self._recompute_fast_path()
        resuming_phase = self.pending_phase
        tool_break = False
        async for event in self.resume_phase(tool_outputs):
            if event[0] == "tool_calls":
                tool_break = True
            yield event
        if tool_break:
            return
        if resuming_phase == "synthesis":
            return  # resume_phase ya completó la síntesis, no hay nada más
        if self.fast_path_active:
            return  # la hoja atómica ya fue la respuesta final
        async for event in self._execute_remaining():
            if event[0] == "tool_calls":
                tool_break = True
            yield event
        if tool_break:
            return
        for event in self._progress_events(
            "Fase 3 de 3. Listo, todas las tareas atómicas trabajadas correctamente, procedo a dar la respuesta final.\n"
        ):
            yield event
        async for event in self.synthesize_final():
            yield event


    async def build_task_tree(self) -> AsyncIterator[Event]:
        """Fase 1: descompone la instrucción del turno en un árbol de tareas
        atómicas, aplicando los límites de profundidad, subtareas y totales."""
        assert self.goal_ctx is not None
        self._ensure_providers()
        self._bind_client_telemetry()
        self.budget.register_tasks_created(1)  # la raíz también es una tarea
        self.root = TaskNode(description=self.goal_ctx.turn_instruction, depth=0)
        queue: List[TaskNode] = [self.root]
        while queue:
            node = queue.pop(0)
            for event in self._progress_events(f"Clasificando: {_short(node.description)}…\n"):
                yield event
            await self._plan_node(node)
            queue.extend(node.children)
        self.leaves = _collect_atomic_leaves(self.root)
        self.metrics.subtasks = len(self.leaves)
        self._compute_prereqs()
        self._recompute_fast_path()
        tree_lines = _render_tree(self.root)
        if tree_lines:
            for event in self._detail_events("\n".join(tree_lines) + "\n\n"):
                yield event


