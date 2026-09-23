"""Lectura y escritura segura del `.env` (docs/UI_IMPLEMENTACION.md §7, Paso 3).

Garantías de este módulo:

* ``read_env`` → dict clave=valor **en el orden del fichero**; ignora
  comentarios y líneas vacías; no parte los valores que contienen ``=``
  (p. ej. el JSON de ``MODEL_PRICES``) y elimina comillas exteriores como
  haría python-dotenv.
* ``write_env`` →
  1. backup automático ``.env.bak-YYYYmmdd-HHMMSS`` (máx. 10, se rotan);
     **si el backup falla no se escribe nada** (``EnvFileError``);
  2. reescribe SOLO las claves enviadas: el resto de líneas, comentarios y
     orden se preservan intactos; una clave comentada (``#KEY=…``) que se
     envía se reactiva; una clave ausente se añade dentro de la sección de
     su grupo (``# --- Grupo ---``), o al final con cabecera nueva si ese
     grupo todavía no tiene sección;
  3. escritura atómica (tmp + ``fsync`` + ``os.replace``): nunca queda un
     `.env` a medias.
* ``reset_fields`` → escribe el default del catálogo para cada clave pedida;
  un default ``None`` (p. ej. ``EXPOSE_REASONING_CONTENT``) se deja
  **comentado** para que ``Settings`` aplique su default.

Seguridad: claves ``[A-Za-z_][A-Za-z0-9_]*`` y valores sin saltos de línea
— una llamada inválida lanza ``EnvFileError`` ANTES de tocar el fichero.

La validación de semántica (rangos, opciones, patrones) no vive aquí: la
hace ``app/web/schemas.py`` (Paso 2) antes de llamarnos; este módulo solo
garantiza estructura y atomicidad del fichero.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Solo para localizar el GRUPO al insertar una clave ausente (write_env no
# recibe el catálogo por firma; reset_fields recibe el suyo como parámetro).
from . import catalog as default_catalog

_KEY_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
_LINE_RE = re.compile(r"\A\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)\Z")
_HEADER_RE = re.compile(r"\A#\s*-{2,}\s*(\S.*?)\s*\Z")
_TAIL_DASHES_RE = re.compile(r"\s*-{2,}\s*\Z")

MAX_BACKUPS = 10

# Alias de secciones del `.env` por grupo del catálogo. La clave primaria de
# cada grupo es SU PRIMERA PALABRA (p. ej. "upstream", "sesiones"); aquí solo
# se añaden alias para que una clave ausente caiga en la sección temática
# correcta aunque el comentario no empiece por el nombre del grupo (p. ej.
# PLANNER_MODEL → "# --- Temperaturas internas … ---").
_SECTION_ALIASES: Dict[str, Tuple[str, ...]] = {
    "Descomposición y rondas": ("rondas",),
    "Presupuesto de ejecución": ("compresión", "compresion"),
    "Modelos y proveedores por fase": ("temperaturas", "proveedor"),
    "Fast path y perfiles": ("perfil",),
    "Verificación y paralelismo": ("paralelismo",),
    "Observabilidad": ("trazas", "precio"),
}


class EnvFileError(Exception):
    """Error estructural al leer/escribir el `.env` (la UI responde 500)."""


def read_env(path) -> Dict[str, str]:
    """Claves activas del `.env` en orden de aparición (comentarios, no)."""
    file_path = Path(path)
    if not file_path.exists():
        return {}
    env: Dict[str, str] = {}
    for line in file_path.read_text(encoding="utf-8").splitlines():
        match = _LINE_RE.match(line)
        if not match:
            continue  # comentarios, cabeceras de sección y líneas vacías
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[match.group(1)] = value
    return env


def _header_body(line: str) -> Optional[str]:
    """Texto de una cabecera de sección (`# --- Upstream (…) ---`) o None."""
    match = _HEADER_RE.match(line)
    if not match:
        return None
    body = _TAIL_DASHES_RE.sub("", match.group(1)).strip()
    return body or None


def _tokens_for(group: str) -> Tuple[str, ...]:
    """Primera palabra del grupo (primaria) + alias del mapa de secciones."""
    primary = group.split()[0].lower()
    aliases = tuple(t for t in _SECTION_ALIASES.get(group, ()) if t != primary)
    return (primary,) + aliases


def _backup(path: Path) -> Path:
    """Copia `.env.bak-YYYYmmdd-HHMMSS` (única) y rota. Error → EnvFileError."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    suffix = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak-{stamp}-{suffix}")
        suffix += 1
    try:
        shutil.copy2(path, backup)
    except OSError as exc:
        raise EnvFileError(f"no se pudo crear el backup del .env: {exc}") from exc
    _rotate_backups(path)
    return backup


def _rotate_backups(path: Path, keep: int = MAX_BACKUPS) -> None:
    """Conserva los ``keep`` backups más recientes (best-effort)."""
    try:
        stale = sorted(path.parent.glob(f"{path.name}.bak-*"))
        for old in stale[:-keep]:
            old.unlink()
    except OSError:
        pass  # no bloquear la escritura por no poder borrar un backup viejo
BACKUP_KEEP = MAX_BACKUPS  # alias público usado por los tests (10)


# ------------------------------------------------------------------
# Formato de valores para .env
# ------------------------------------------------------------------

def _format_value(key: str, value: str) -> str:
    """Serializa un valor para el ``.env``.

    Un valor que contenga espacios/tabs se envuelve en comillas dobles
    (el resto se escribe tal cual). ``read_env`` desenmascara las comillas
    exteriores, por lo que un JSON como ``{"m": {"input": 1}}`` vuelve a
    parsear exactamente igual.
    """
    text = str(value)
    if any(c.isspace() for c in text):
        return f'"{text}"'
    return text


def _default_to_envstr(default: Any) -> Optional[str]:
    """Default del catálogo → texto .env (\"true\"/\"false\" para bool)."""
    if default is None:
        return None
    if isinstance(default, bool):
        return "true" if default else "false"
    return str(default)


def _active_key_line(key: str, value: str) -> str:
    return f"{key}={_format_value(key, value)}"


# ------------------------------------------------------------------
# Secciones / posicionamiento
# ------------------------------------------------------------------

_COMMENTED_KEY_RE = re.compile(r"\A\s*#\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\Z")


def _section_header_for(group: str) -> str:
    return f"# --- {group} ---"


def _find_section_index(lines: List[str], tokens: Tuple[str, ...]) -> Optional[int]:
    """Índice de la primera cabecera cuyo cuerpo contiene alguno de los tokens."""
    for i, line in enumerate(lines):
        body = _header_body(line)
        if body is None:
            continue
        body_tokens = tuple(body.lower().split())
        if any(tok in body_tokens for tok in tokens):
            return i
    return None


def _append_missing_keys(lines: List[str], catalog, key_values: Dict[str, Optional[str]]) -> List[str]:
    """Coloca claves ausentes dentro de la sección de su grupo (creándola si
    no existe). ``key_values`` mapea cada clave al valor .env a escribir
    (None → línea comentada / borrosa). Devuelve la nueva lista de líneas."""
    by_key = {p.key: p for p in catalog.PARAMS}
    ordered = [k for k in (by_key[p.key].key for p in catalog.PARAMS) if k in key_values]
    pending = ordered[:]
    while pending:
        group = by_key[pending[0]].group
        tokens = _tokens_for(group)
        idx = _find_section_index(lines, tokens)
        if idx is None:
            header = _section_header_for(group)
            if not (lines and lines[-1].strip() == header):
                if lines and lines[-1].strip() != "":
                    lines.append("")
                lines.append(header)
            insert_at = len(lines)
        else:
            insert_at = idx + 1
        i = insert_at
        for key in pending[:]:
            if by_key[key].group != group:
                continue
            value = key_values[key]
            if value is not None:
                lines.insert(i, _active_key_line(key, value))
                i += 1
            else:
                lines.insert(i, f"# {key}")
            pending.remove(key)
    return lines
# ------------------------------------------------------------------
# Escritura pública
# ------------------------------------------------------------------

def _atomic_write(file_path: Path, text: str) -> None:
    """fsync + os.replace: garantiza que nunca quede un ``.env`` a medias.

    Falla con ``RuntimeError`` si el paso de escritura falla (p.ej. disco
    lleno); el fichero original nunca es tocado en ese caso.
        """
    tmp = file_path.with_name(".env.tmp")
    content_bytes = "\n".join(text.splitlines()) + "\n"
    try:
        tmp.write_bytes(content_bytes.encode("utf-8"))
    except OSError as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise RuntimeError(f"no se pudo escribir el .env: {exc}") from exc
    # fsync es best-effort: en Windows puede fallar sobre handles de tmp
    # sin que eso impida el os.replace (el reemplazo atómico es la garantía
    # real de integridad).
    try:
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
    except OSError:
        pass
    try:
        os.replace(tmp, file_path)
    except OSError as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise RuntimeError(f"no se pudo aplicar el .env: {exc}") from exc


def write_env(path, updates: Dict[str, str]) -> Optional[Path]:
    """Reescribe SOLO las claves enviadas en ``updates``.

    Reglas (docs/UI_IMPLEMENTACION.md §7, Paso 3):
      1. backup automático ``.env.bak-YYYYmmdd-HHMMSS`` (máx. 10, rotar);
         si el backup falla se lanza ``EnvFileError`` y **no se escribe nada**.
      2. el resto de líneas, comentarios y orden se preservan intactos;
         una clave comentada que se envía se reactiva; una clave ausente
         se añade dentro de la sección de su grupo (o al final con su propia
         cabecera si ese grupo todavía no tiene sección).
      3. escritura atómica (tmp + fsync + os.replace).

    Devuelve la ruta del backup creado (``None`` si el fichero no existía,
    es decir si no hubo nada que respaldar) para que el caller pueda revertir
    exactamente ese estado si la recarga de la configuración falla.
    """
    # Validar estructura ANTES de tocar el disco.
    for key, value in updates.items():
        if not _KEY_RE.match(key):
            raise EnvFileError(f"clave inválida para .env: {key!r}")
        if value is None:
            raise EnvFileError(f"valor None para {key!r}")
        if "\n" in str(value) or "\r" in str(value):
            raise EnvFileError(f"valor con salto de línea para {key!r}")

    file_path = Path(path)
    if not file_path.exists():
        lines: List[str] = []
        backup: Optional[Path] = None
    else:
        backup = _backup(file_path)  # EnvFileError si falla → nada se escribe
        lines = file_path.read_text(encoding="utf-8").splitlines()

    # Aplicar actualizaciones sobre las líneas existentes.
    placed: set = set()
    out: List[str] = []
    for line in lines:
        m = _LINE_RE.match(line)
        cm = _COMMENTED_KEY_RE.match(line)
        if m:
            key = m.group(1)
            if key in updates:
                out.append(_active_key_line(key, updates[key]))
                placed.add(key)
            else:
                out.append(line)
            continue
        if cm and cm.group(1) in updates:
            key = cm.group(1)
            out.append(_active_key_line(key, updates[key]))
            placed.add(key)
            continue
        out.append(line)

    # Añadir claves ausentes en su sección de grupo.
    missing = [k for k in updates if k not in placed]
    if missing:
        _append_missing_keys(out, default_catalog, {k: updates[k] for k in missing})

    _atomic_write(file_path, "\n".join(out))
    return backup


def restore(path, text: Optional[str]) -> None:
    """Devuelve el ``.env`` a un contenido previo exacto (``None`` = borrarlo).

    Lo usa ``routes.py`` para revertir un PUT cuya configuración resultó no
    parsear: no es una edición nueva, es un rollback, así que **no crea
    backup** ni toca la rotación (el backup del intento fallido se queda,
    como cualquier otro, sujeto al máximo de 10).
    """
    file_path = Path(path)
    if text is None:
        if file_path.exists():
            file_path.unlink()
        return
    _atomic_write(file_path, text)


def reset_fields(path, keys: List[str], catalog=None) -> None:
    """Escribe el default del catálogo para cada clave pedida.

    - un default ``None`` (p.ej. ``EXPOSE_REASONING_CONTENT``) se **borra**
      (la línea activa se comenta) para que ``Settings`` aplique su default;
    - cualquier otra clave se escribe/reactiva con su default.
    Lanza ``KeyError`` si alguna clave no está en el catálogo.
    """
    catalog = catalog or default_catalog
    by_key = {p.key: p for p in catalog.PARAMS}
    for key in keys:
        if key not in by_key:
            raise KeyError(key)

    file_path = Path(path)
    if not file_path.exists():
        # Nada que resetear: crear con los defaults de las claves.
        out_lines: List[str] = []
        lines: List[str] = []
        handled: set = set()
    else:
        _backup(file_path)  # EnvFileError si falla → nada se escribe
        lines = file_path.read_text(encoding="utf-8").splitlines()
        out_lines = []
        handled = set()

    pending_none = {k for k in keys if by_key[k].default is None}

    for line in lines:
        m = _LINE_RE.match(line)
        cm = _COMMENTED_KEY_RE.match(line)
        if m:
            key = m.group(1)
            if key in pending_none:
                out_lines.append(f"# {key}")  # default None → borrar
                handled.add(key)
            elif key in keys:
                out_lines.append(
                    _active_key_line(key, _default_to_envstr(by_key[key].default))
                )
                handled.add(key)
            else:
                out_lines.append(line)
            continue
        if cm and cm.group(1) in keys and cm.group(1) not in pending_none:
            key = cm.group(1)
            out_lines.append(
                _active_key_line(key, _default_to_envstr(by_key[key].default))
            )
            handled.add(key)
            continue
        out_lines.append(line)

    missing = [k for k in keys if k not in handled and k not in pending_none]
    if missing:
        _append_missing_keys(out_lines, catalog, {k: _default_to_envstr(by_key[k].default) for k in missing})

    _atomic_write(file_path, "\n".join(out_lines))
