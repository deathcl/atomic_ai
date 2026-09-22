"""Tests de la UI (docs/UI_IMPLEMENTACION.md — Paso 1: catálogo único)."""
from typing import Literal, get_args, get_origin

from pydantic.fields import FieldInfo

from app.config import Settings
from app.web import catalog


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
