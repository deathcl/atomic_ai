"""Compresión inteligente del contexto acumulado.

En vez de mandar a cada tarea todos los resultados anteriores sin límite, el
motor mantiene completos los resultados recientes y condensa los antiguos de
forma extractiva (sin llamadas extra al modelo): conserva las líneas con datos
críticos —rutas, nombres, decisiones, errores, puertos, URLs— y recorta el
resto dejando una marca de lo omitido.
"""

from __future__ import annotations

import re
from typing import List, Sequence, Tuple

# Líneas que casi nunca conviene perder al comprimir.
_CRITICAL_PATTERN = re.compile(
    r"(?:"
    r"[/\\][\w.\-/\\]+"  # rutas
    r"|\b\w+\.(?:py|js|ts|tsx|jsx|json|ya?ml|toml|ini|env|md|txt|sql|html|css|sh|bat)\b"  # archivos
    r"|\b(?:def|class|function|import|from|return|const|let|var|interface|struct)\b"
    r"|\b(?:error|errors?|excepci[oó]n|traceback|fallo|failed|failure|timeout|warning)\b"
    r"|\b(?:decid|decidid|decisi[oó]n|acordad|acuerdo|regla|convenci[oó]n)\b"
    r"|\b(?:nombre|name|id|clave|key|token|puerto|port|url|endpoint|host|schema|tabla|table)\b"
    r"|\b(?:l[ií]mite|budget|presupuesto|coste|cost|latencia|latency)\b"
    r"|\b(?:TODO|FIXME|NECESITA_HERRAMIENTA|NO_EJECUTADA|L[IÍ]MITE)\b"
    r"|\b\d{2,}\b"
    r")",
    re.IGNORECASE,
)

COMPRESSION_MARKER = "[…{omitted} caracteres resumidos del resultado «{label}»…]"


def _is_critical(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return bool(_CRITICAL_PATTERN.search(stripped))


def condense_result(text: str, limit: int, *, label: str = "tarea") -> str:
    """Resumen extractivo determinista de un resultado largo."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text

    marker = COMPRESSION_MARKER.format(omitted=len(text) - limit, label=label)
    budget = max(80, limit - len(marker))
    head_len = max(40, budget // 2)
    tail_len = max(20, budget // 4)
    critical_budget = max(0, budget - head_len - tail_len)

    critical_lines: List[str] = []
    used = 0
    for line in text.splitlines():
        if not _is_critical(line):
            continue
        candidate = line.strip()
        if used + len(candidate) + 1 > critical_budget:
            break
        critical_lines.append(candidate)
        used += len(candidate) + 1

    parts = [text[:head_len].rstrip()]
    if critical_lines:
        parts.append("\n".join(critical_lines))
    if tail_len:
        parts.append(text[-tail_len:].lstrip())
    parts.append(marker)
    return "\n".join(part for part in parts if part)


def render_results(
    entries: Sequence[Tuple[str, str]],
    max_chars: int,
    *,
    recent_keep: int = 3,
    empty_placeholder: str = "(sin resultados)",
) -> str:
    """Une ``(etiqueta, texto)`` respetando el presupuesto de contexto.

    Los ``recent_keep`` resultados más nuevos se mantienen completos si caben;
    el resto se condensa, y si aun así no cabe todo, se omiten los más viejos
    dejando constancia explícita de cuántos se omitieron.
    """
    if not entries:
        return empty_placeholder

    labels = [label for label, _ in entries]
    texts = [text for _, text in entries]
    if max_chars <= 0 or sum(len(text) for text in texts) <= max_chars:
        return "\n".join(texts)

    kept: List[str] = []
    remaining = max_chars
    total = len(texts)
    for position, text in enumerate(reversed(texts)):
        label = labels[total - 1 - position]
        if position < recent_keep and len(text) <= max(remaining, 400):
            kept.append(text)
            remaining -= len(text)
            continue
        share = max(240, remaining // max(1, total - position))
        condensed = condense_result(text, share, label=label)
        kept.append(condensed)
        remaining -= len(condensed)
        if remaining <= 0 and position < total - 1:
            omitted = total - position - 1
            kept.append(f"[…{omitted} resultados anteriores omitidos por límite de contexto…]")
            break

    result = "\n".join(reversed(kept))
    hard_cap = int(max_chars * 1.2) + 200
    if len(result) > hard_cap:
        result = result[:hard_cap] + "\n[…contexto recortado por límite de contexto…]"
    return result
