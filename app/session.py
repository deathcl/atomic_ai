from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from .engine import GoalContext, TaskNode


def _message_digest(prev: str, message: dict[str, Any]) -> str:
    serialized = json.dumps(message, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{prev}\x1e{serialized}".encode("utf-8")).hexdigest()


def hash_chain(messages: list[dict[str, Any]]) -> list[str]:
    """Hash acumulado por prefijo: chain[i] identifica de forma estable el
    historial messages[:i+1], para poder detectar cuándo una request nueva es
    continuación exacta (mismo prefijo) de una conversación ya vista."""
    chain: list[str] = []
    prev = ""
    for message in messages:
        prev = _message_digest(prev, message)
        chain.append(prev)
    return chain


def new_session_id() -> str:
    return uuid.uuid4().hex


@dataclass
class SessionState:
    session_id: str
    checkpoint_hash: str
    checkpoint_len: int
    goal_ctx: GoalContext
    model: str
    tools: Optional[list[dict[str, Any]]]
    tool_choice: Any
    root: TaskNode
    leaves: list[TaskNode]
    results: list[str]
    pending_phase: Optional[Literal["leaf", "synthesis"]] = None
    pending_leaf_index: Optional[int] = None
    pending_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    pending_conversation: list[dict[str, Any]] = field(default_factory=list)
    tool_round_count: int = 0
    # Solo el contenido final de cada síntesis ya entregada en turnos previos
    # de esta misma conversación — nunca el reasoning_content interno, para
    # no filtrar la narración del proxy como si fuera diálogo real.
    turn_history: list[str] = field(default_factory=list)
    last_used_at: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def pending_tool_call_ids(self) -> set[str]:
        return {tc["id"] for tc in self.pending_tool_calls if tc.get("id")}

    def to_dict(self) -> dict[str, Any]:
        """Serialización completa (sin el lock) para persistir la sesión."""
        return {
            "session_id": self.session_id,
            "checkpoint_hash": self.checkpoint_hash,
            "checkpoint_len": self.checkpoint_len,
            "goal_ctx": self.goal_ctx.to_dict(),
            "model": self.model,
            "tools": self.tools,
            "tool_choice": self.tool_choice,
            "root": self.root.to_dict(),
            "leaves": [leaf.to_dict() for leaf in self.leaves],
            "results": list(self.results),
            "pending_phase": self.pending_phase,
            "pending_leaf_index": self.pending_leaf_index,
            "pending_tool_calls": self.pending_tool_calls,
            "pending_conversation": self.pending_conversation,
            "tool_round_count": self.tool_round_count,
            "turn_history": list(self.turn_history),
            "last_used_at": self.last_used_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionState":
        from .engine import TaskNode

        state = cls(
            session_id=str(data.get("session_id") or ""),
            checkpoint_hash=str(data.get("checkpoint_hash") or ""),
            checkpoint_len=int(data.get("checkpoint_len") or 0),
            goal_ctx=GoalContext.from_dict(data.get("goal_ctx")),
            model=str(data.get("model") or ""),
            tools=data.get("tools"),
            tool_choice=data.get("tool_choice"),
            root=TaskNode.from_dict(root_data),
            leaves=[TaskNode.from_dict(leaf) for leaf in (data.get("leaves") or [])],
            results=[str(r) for r in (data.get("results") or [])],
            pending_phase=data.get("pending_phase"),
            pending_leaf_index=data.get("pending_leaf_index"),
            pending_tool_calls=list(data.get("pending_tool_calls") or []),
            pending_conversation=list(data.get("pending_conversation") or []),
            tool_round_count=int(data.get("tool_round_count") or 0),
            turn_history=[str(t) for t in (data.get("turn_history") or [])],
            last_used_at=float(data.get("last_used_at") or time.time()),
        )
        return state


def is_valid_resume(session: SessionState, messages: list[dict[str, Any]]) -> bool:
    """True si `messages` extiende exactamente el checkpoint de `session` con
    los resultados de tool que esa sesión estaba esperando (en una hoja
    atómica o en la síntesis final — ambas quedan marcadas con pending_phase)."""
    if session.pending_phase is None or not session.pending_tool_calls:
        return False
    if len(messages) <= session.checkpoint_len:
        return False
    suffix = messages[session.checkpoint_len :]
    tool_ids = {m.get("tool_call_id") for m in suffix if m.get("role") == "tool" and m.get("tool_call_id")}
    return session.pending_tool_call_ids.issubset(tool_ids)


def is_new_turn(session: SessionState, messages: list[dict[str, Any]]) -> bool:
    """True si la sesión ya terminó su run (sin fase pendiente) pero la
    request trae mensajes nuevos más allá del checkpoint: un turno externo
    nuevo del caller sobre una conversación ya resuelta, no una reanudación
    de tool call. Permite sembrar el turno nuevo con turn_history en vez de
    reaplanar/redecomponer el historial crudo desde cero."""
    return session.pending_phase is None and len(messages) > session.checkpoint_len


def extract_tool_outputs(session: SessionState, messages: list[dict[str, Any]]) -> dict[str, str]:
    suffix = messages[session.checkpoint_len :]
    outputs: dict[str, str] = {}
    for m in suffix:
        if m.get("role") == "tool" and m.get("tool_call_id") in session.pending_tool_call_ids:
            outputs[m["tool_call_id"]] = m.get("content") or ""
    return outputs


class SessionStore:
    """Guarda el árbol de tareas y los resultados ya calculados entre
    peticiones HTTP, para poder reanudar un turno externo pausado por una
    tool call sin repetir la descomposición ni las tareas atómicas ya
    resueltas."""

    def __init__(self, ttl_seconds: float, max_sessions: int) -> None:
        self._ttl = ttl_seconds
        self._max_sessions = max_sessions
        self._sessions: dict[str, SessionState] = {}
        self._lock = asyncio.Lock()

    async def find_matching(self, messages: list[dict[str, Any]]) -> Optional[SessionState]:
        async with self._lock:
            self._evict_expired_locked()
            candidates = list(self._sessions.values())

        if not candidates or not messages:
            return None

        chain = hash_chain(messages)
        best: Optional[SessionState] = None
        for session in candidates:
            if session.checkpoint_len <= 0 or session.checkpoint_len > len(chain):
                continue
            if chain[session.checkpoint_len - 1] != session.checkpoint_hash:
                continue
            if best is None or session.checkpoint_len > best.checkpoint_len:
                best = session
        return best

    async def save(self, session: SessionState) -> None:
        session.last_used_at = time.time()
        async with self._lock:
            self._sessions[session.session_id] = session
            self._evict_expired_locked()
            overflow = len(self._sessions) - self._max_sessions
            if overflow > 0:
                oldest = sorted(self._sessions.values(), key=lambda s: s.last_used_at)
                for stale in oldest[:overflow]:
                    self._sessions.pop(stale.session_id, None)

    def _evict_expired_locked(self) -> None:
        if self._ttl <= 0:
            return
        cutoff = time.time() - self._ttl
        expired = [sid for sid, s in self._sessions.items() if s.last_used_at < cutoff]
        for sid in expired:
            self._sessions.pop(sid, None)


class SqliteSessionStore(SessionStore):
    """SessionStore persistente en SQLite: sobrevive a reinicios del proceso
    y permite compartir sesiones entre workers. El índice en memoria se
    mantiene como caché de lectura; la escritura es siempre doble (memoria +
    disco)."""

    def __init__(self, ttl_seconds: float, max_sessions: int, database_path: str) -> None:
        super().__init__(ttl_seconds=ttl_seconds, max_sessions=max_sessions)
        self._database_path = database_path
        parent = Path(database_path).parent
        if str(parent):
            parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                checkpoint_len INTEGER NOT NULL,
                last_used_at REAL NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        self._connection.commit()
        self._load_all()

    def _load_all(self) -> None:
        cursor = self._connection.execute(
            "SELECT payload FROM sessions ORDER BY last_used_at DESC"
        )
        for (payload,) in cursor.fetchall():
            try:
                state = SessionState.from_dict(json.loads(payload))
            except Exception:  # pragma: no cover - fila corrupta: se ignora
                continue
            self._sessions[state.session_id] = state

    async def save(self, session: SessionState) -> None:
        await super().save(session)
        payload = json.dumps(session.to_dict(), ensure_ascii=False, default=str)
        self._connection.execute(
            """
            INSERT INTO sessions (session_id, checkpoint_len, last_used_at, payload)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                checkpoint_len = excluded.checkpoint_len,
                last_used_at = excluded.last_used_at,
                payload = excluded.payload
            """,
            (session.session_id, session.checkpoint_len, session.last_used_at, payload),
        )
        self._connection.commit()
