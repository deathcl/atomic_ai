"""Tests de la UI (docs/UI_IMPLEMENTACION.md — Pasos 1 y 2: catálogo y schemas)."""
import json
from typing import Literal, get_args, get_origin

import pytest
from pydantic import ValidationError
from pydantic.fields import FieldInfo

from app.config import Settings
from app.web import catalog
from app.web.schemas import ConfigUpdate, env_updates


def _resolved_default(fi: FieldInfo):
    if fi.default_factory is not None:
        return fi.default_factory()
    return fi.default


def test_catalog_matches_settings():
    """Catálogo ≡ Settings: mismos campos y mismos defaults (§7, Paso 1)."""
    fields = Settings.model_fields
    specs = {s.field: s for s in catalog.PARAMS}
    assert len(specs) == len(catalog.PARAMS), "campo duplicado en PARAMS"
    missing = sorted(set(fields) - set(specs))
    extra = sorted(set(specs) - set(fields))
    assert not missing, f"campos de Settings sin catalogar: {missing}"
    assert not extra, f"campos en el catálogo que no existen en Settings: {extra}"

    for name, fi in fields.items():
        spec = specs[name]
        assert spec.default == _resolved_default(fi), f"default divergido en {name}"
        assert spec.group in catalog.GROUPS, f"grupo desconocido en {name}: {spec.group!r}"
        assert spec.widget in catalog.WIDGETS, f"widget inválido en {name}: {spec.widget!r}"
        assert spec.key == name.upper(), f"key derivada mal en {name}"
        assert spec.label and spec.description, f"falta label/description en {name}"


def test_select_options_are_closed_and_match_literal():
    """Todo select tiene opciones cerradas; si Settings es Literal, coinciden."""
    selects = [s for s in catalog.PARAMS if s.widget == "select"]
    assert selects, "debe haber selects catalogados"
    for spec in selects:
        assert spec.options, f"select sin opciones cerradas: {spec.field}"
        ann = Settings.model_fields[spec.field].annotation
        if get_origin(ann) is Literal:
            assert spec.options == tuple(get_args(ann)), (
                f"opciones divergentes del Literal de Settings en {spec.field}"
            )


def test_numeric_widgets_have_bounds_and_valid_defaults():
    for spec in catalog.PARAMS:
        if spec.widget in ("int", "float"):
            assert spec.minimum is not None and spec.maximum is not None, spec.field
            assert spec.minimum <= spec.maximum, spec.field
            if spec.default is not None:
                assert spec.minimum <= spec.default <= spec.maximum, (
                    f"default fuera de rango en {spec.field}"
                )


def test_secrets_and_json_widget():
    secrets = {s.field for s in catalog.PARAMS if s.secret}
    assert secrets == {
        "upstream_api_key",
        "planner_api_key",
        "executor_api_key",
        "synthesis_api_key",
        "verification_api_key",
    }
    prices = next(s for s in catalog.PARAMS if s.field == "model_prices")
    assert prices.widget == "json"


# --- Paso 2: ConfigUpdate generado del catálogo -----------------------------

def test_config_update_enforces_catalog_rules():
    """select cerrado, cotas, campos opcionales de fase y extra=forbid (§5.2)."""
    # opción fuera del select inmutable
    with pytest.raises(ValidationError):
        ConfigUpdate(trace_mode="verbose")
    # fuera de cotas
    with pytest.raises(ValidationError):
        ConfigUpdate(max_subtasks_per_node=999)
    # campo desconocido (typo) → 422, no se ignora en silencio
    with pytest.raises(ValidationError):
        ConfigUpdate(nope=1)
    # campo de fase opcional: vacío = usar fallback (§5.1)
    assert ConfigUpdate(planner_model="").planner_model == ""
    # la URL base NO es opcional: vacío → 422
    with pytest.raises(ValidationError):
        ConfigUpdate(upstream_base_url="")


def test_config_update_model_prices():
    ok = ConfigUpdate(model_prices={"deepseek-v4-flash": {"input": 0.5, "output": 2.0}})
    assert ok.model_prices is not None
    # clave desconocida dentro del precio (§5.3 ejemplo) → 422
    with pytest.raises(ValidationError):
        ConfigUpdate(model_prices={"m": {"entrada": 1}})


def test_env_updates_formats_for_envfile():
    """Conversión validada → pares .env (Paso 3 los consumirá)."""
    updates = env_updates(
        ConfigUpdate(
            trace_mode="off",
            expose_metrics=False,
            planner_temperature=0.2,
            model_prices={"m": {"input": 1}},
        )
    )
    assert updates["TRACE_MODE"] == "off"
    assert updates["EXPOSE_METRICS"] == "false"
    assert updates["PLANNER_TEMPERATURE"] == "0.2"
    # JSON compacto, sin nulos, re-parseable por Settings
    assert json.loads(updates["MODEL_PRICES"]) == {"m": {"input": 1.0}}
    # no enviado → no aparece (no se toca esa clave del .env)
    assert "PLANNER_MODEL" not in updates
