"""Clasificación de herramientas por riesgo.

``parallel`` usa esta clasificación para no lanzar en paralelo tareas que
podrían ejecutar herramientas con efectos secundarios (escritura, borrado,
publicación externa o comandos), donde el orden y la exclusividad importan.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple

Risk = str  # "read" | "write" | "destructive" | "external" | "unknown"

_DESTRUCTIVE_KEYWORDS = (
    "delete",
    "remove",
    "rmdir",
    "rm_",
    "unlink",
    "drop",
    "truncate",
    "purge",
    "destroy",
    "format",
    "kill",
    "borrar",
    "eliminar",
    "destruir",
)
_WRITE_KEYWORDS = (
    "write",
    "edit",
    "create",
    "mkdir",
    "append",
    "move",
    "rename",
    "copy",
    "chmod",
    "chown",
    "patch",
    "apply",
    "update",
    "replace",
    "insert",
    "escribir",
    "crear",
    "editar",
    "modificar",
    "mover",
    "renombrar",
)
_EXTERNAL_KEYWORDS = (
    "execute",
    "exec",
    "shell",
    "bash",
    "powershell",
    "command",
    "cmd",
    "run_",
    "run ",
    "commit",
    "push",
    "publish",
    "deploy",
    "upload",
    "send",
    "post_",
    "http_post",
    "install",
    "start",
    "stop",
    "restart",
    "publish",
    "webhook",
    "ejecutar",
    "publicar",
    "enviar",
    "instalar",
)
_READ_KEYWORDS = (
    "read",
    "get",
    "list",
    "ls_",
    "search",
    "find",
    "grep",
    "glob",
    "view",
    "show",
    "stat",
    "cat",
    "head",
    "tail",
    "fetch",
    "query",
    "inspect",
    "describe",
    "status",
    "diff",
    "leer",
    "listar",
    "buscar",
    "consultar",
    "obtener",
    "ver",
    "mostrar",
)

# Riesgos que impiden paralelizar tareas (dos efectos secundarios concurrentes
# pueden pisarse entre sí).
SIDE_EFFECT_RISKS = ("write", "destructive", "external")


def tool_name(tool: Dict[str, Any]) -> str:
    function = tool.get("function") or {}
    return str(function.get("name") or "")


def clone_tool_calls(tool_calls: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Copia defensiva de una lista de tool calls (ids y argumentos)."""
    return [dict(call) for call in tool_calls]


def tool_call_ids(tool_calls: Iterable[Dict[str, Any]]) -> List[str]:
    return [str(call.get("id")) for call in tool_calls if call.get("id")]


def classify_tool(tool: Dict[str, Any]) -> Risk:
    name = tool_name(tool).lower()
    description = str((tool.get("function") or {}).get("description") or "").lower()
    haystack = f"{name} {description}"
    for keyword in _DESTRUCTIVE_KEYWORDS:
        if keyword in haystack:
            return "destructive"
    for keyword in _EXTERNAL_KEYWORDS:
        if keyword in haystack:
            return "external"
    for keyword in _WRITE_KEYWORDS:
        if keyword in haystack:
            return "write"
    for keyword in _READ_KEYWORDS:
        if keyword in haystack:
            return "read"
    return "unknown"


def has_side_effect_tools(tools: Iterable[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    """``(True, nombres)`` si alguna herramienta tiene efectos secundarios."""
    flagged: List[str] = []
    for tool in tools or []:
        if classify_tool(tool) in SIDE_EFFECT_RISKS:
            flagged.append(tool_name(tool) or "?")
    return bool(flagged), flagged
