"""Validación del PUT de configuración, derivada del catálogo (§5.2–5.3).

Regla fundamental: NINGUNA regla de validación se escribe a mano. El modelo
ConfigUpdate se genera a partir de catalog.PARAMS:

    select          → Literal de las opciones CERRADAS e inmutables del catálogo
    toggle          → bool
    int             → Field(ge=minimum, le=maximum) con las cotas del catálogo
    float           → Field(ge=minimum, le=maximum)
    json            → ModelPrices (§5.3, claves extra prohibidas)
    text/password/… → StringConstraints(max_length) + patrón del catálogo (§5.1)
                       aplicado con re de Python en _pattern_validator

Si mañana añades un parámetro a catalog.py aparece aquí automáticamente: no
hay dos sitios que mantener y el servidor no puede divergir del formulario.

Semántica (§6): campo ausente o `null` = "no enviado" = no se toca su clave en
el .env; para volver al default existe POST /ui/api/config/reset. Un valor
fuera de opciones/rangos/patrón → 422 y el .env NO se escribe.

Consumido por app/web/routes.py (Paso 4); env_updates() entrega los pares
clave=valor ya en formato .env para app/web/envfile.py (Paso 3).
"""
from __future__ import annotations

import json
import re
from typing import Annotated, Dict, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StringConstraints,
    create_model,
    model_validator,
)

from . import catalog
from .catalog import ParamSpec


# --- §5.3: precios por modelo (widget json) ---------------------------------

class PhasePrice(BaseModel):
    """Precio en USD por millón de tokens. Claves desconocidas → 422."""

    model_config = ConfigDict(extra="forbid")  # nada de claves raras: "entrada" ✗

    input: Optional[Annotated[float, Field(ge=0, le=10_000)]] = None
    output: Optional[Annotated[float, Field(ge=0, le=10_000)]] = None
    prompt: Optional[Annotated[float, Field(ge=0, le=10_000)]] = None
    completion: Optional[Annotated[float, Field(ge=0, le=10_000)]] = None


class ModelPrices(RootModel[Dict[str, PhasePrice]]):
    """{"gpt-4o": {"input": 2.5, "output": 10.0}} — se serializa a dict plano."""


# --- Generación desde el catálogo (§5.2) ------------------------------------

_TEXT_WIDGETS = ("text", "password", "url", "path")


def _annotation_for(p: ParamSpec):
    if p.widget == "select":
        return Literal[tuple(p.options)]
    if p.widget == "toggle":
        return bool
    if p.widget == "int":
        return Annotated[int, Field(ge=p.minimum, le=p.maximum)]
    if p.widget == "float":
        return Annotated[float, Field(ge=p.minimum, le=p.maximum)]
    if p.widget == "json":
        return ModelPrices
    # text | password | url | path — la COTA va en el tipo; el PATRÓN se
    # aplica en _pattern_validator con re de Python (ver nota allí).
    return Annotated[str, StringConstraints(max_length=p.max_length)]


def _pattern_validator(model):
    """§5.1: aplica el patrón exacto del catálogo a cada campo de texto.

    Se usa re de Python y no StringConstraints(pattern=...) porque los
    patrones del catálogo son Python-style (\\A/\\Z) e incluyen lookahead
    (PATH_RE), y pydantic v2 valida `pattern` con el motor Rust `regex`,
    que no soporta \\Z ni lookaround (SchemaError al construir el modelo).

    Vacío solo si el default del catálogo es "" (= «usar fallback / sin
    key»); en cualquier otro campo '' tampoco cumple el patrón y se rechaza.
    """
    for p in catalog.PARAMS:
        if p.widget not in _TEXT_WIDGETS or not p.pattern:
            continue
        value = getattr(model, p.field)
        if value is None or (value == "" and p.default == ""):
            continue
        if re.match(p.pattern, value) is None:
            raise ValueError(
                f"{p.key}: valor no válido para «{p.label}»"
            )
    return model


def build_config_model() -> type[BaseModel]:
    """Genera ConfigUpdate desde catalog.PARAMS (§5.2 — nunca a mano)."""
    fields = {p.field: (Optional[_annotation_for(p)], None) for p in catalog.PARAMS}
    return create_model(
        "ConfigUpdate",
        __config__=ConfigDict(extra="forbid", protected_namespaces=()),
        __validators__={
            "_check_patterns": model_validator(mode="after")(_pattern_validator),
        },
        **fields,
    )


ConfigUpdate = build_config_model()


def env_updates(model: ConfigUpdate) -> Dict[str, str]:
    """Pares clave=valor ya validados, en formato .env, para envfile.write_env.

    - ausente/null → no se incluye (no se toca esa clave del .env);
    - bool → "true"/"false" (estilo .env.example);
    - float → str con punto decimal ("0.2", "1800.0");
    - ModelPrices → JSON compacto sin nulos;
    - str/int → tal cual.
    """
    updates: Dict[str, str] = {}
    for p in catalog.PARAMS:
        value = getattr(model, p.field)
        if value is None:
            continue
        if isinstance(value, bool):
            updates[p.key] = "true" if value else "false"
        elif isinstance(value, BaseModel):  # ModelPrices
            updates[p.key] = json.dumps(
                value.model_dump(exclude_none=True),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        else:
            updates[p.key] = str(value)
    return updates
