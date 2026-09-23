"""Tests de la UI (docs/UI_IMPLEMENTACION.md — Pasos 1–3: catálogo, schemas y envfile)."""
import json
from typing import Literal, get_args, get_origin

import pytest
from pydantic import ValidationError
from pydantic.fields import FieldInfo

from app.config import Settings, settings
from app.web import catalog, envfile, routes
from app.web.registry import registry
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
        "ui_token",
    }
    prices = next(s for s in catalog.PARAMS if s.field == "model_prices")
    assert prices.widget == "json"


def test_ui_token_pattern_is_enforced():
    """UI_TOKEN: vacío permitido (sin auth); definido exige 16-128 chars."""
    assert ConfigUpdate(ui_token="").ui_token == ""
    long_token = "a" * 16
    assert ConfigUpdate(ui_token=long_token).ui_token == long_token
    with pytest.raises(ValidationError):
        ConfigUpdate(ui_token="corto")          # < 16
    with pytest.raises(ValidationError):
        ConfigUpdate(ui_token="a" * 129)        # > 128
    with pytest.raises(ValidationError):
        ConfigUpdate(ui_token="a" * 15 + "!")   # carácter fuera de [A-Za-z0-9_-]


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


# --- Paso 3: envfile (escritura segura del .env) -----------------------------

_SAMPLE = """# --- Upstream ---
UPSTREAM_BASE_URL=https://api.example.com
UPSTREAM_MODEL=model-x

# --- Observabilidad ---
EXPOSE_METRICS=true
"""


def _env(tmp_path, text: str = _SAMPLE):
    p = tmp_path / ".env"
    p.write_text(text, encoding="utf-8")
    return p


def test_read_env_parses_and_preserves_order(tmp_path):
    p = _env(tmp_path, _SAMPLE + 'MODEL_PRICES={"m": {"input": 1}}\n')
    data = envfile.read_env(p)
    assert list(data)[:2] == ["UPSTREAM_BASE_URL", "UPSTREAM_MODEL"]
    assert json.loads(data["MODEL_PRICES"]) == {"m": {"input": 1}}


def test_write_env_updates_only_sent_keys(tmp_path):
    p = _env(tmp_path)
    envfile.write_env(p, {"UPSTREAM_MODEL": "otro"})
    text = p.read_text(encoding="utf-8")
    assert "UPSTREAM_MODEL=otro" in text
    # el resto del fichero queda intacto (líneas y comentarios)
    assert "UPSTREAM_BASE_URL=https://api.example.com" in text
    assert "# --- Observabilidad ---" in text
    # backup con el contenido ORIGINAL
    backups = list(tmp_path.glob(".env.bak-*"))
    assert len(backups) == 1
    assert "UPSTREAM_MODEL=model-x" in backups[0].read_text(encoding="utf-8")


def test_write_env_missing_key_goes_to_its_group(tmp_path):
    p = _env(tmp_path)
    # LOG_LEVEL (grupo «Proxy», ausente) y clave existente en su sitio
    envfile.write_env(p, {"LOG_LEVEL": "WARNING", "EXPOSE_METRICS": "false"})
    lines = p.read_text(encoding="utf-8").splitlines()
    i_group = lines.index("# --- Proxy ---")
    assert lines[i_group + 1] == "LOG_LEVEL=WARNING"
    assert "EXPOSE_METRICS=false" in lines


def test_write_env_quotes_and_writes_atomically(tmp_path):
    p = _env(tmp_path)
    envfile.write_env(p, {"MODEL_PRICES": '{"m": {"input": 1}}'})
    line = [l for l in p.read_text(encoding="utf-8").splitlines()
            if l.startswith("MODEL_PRICES=")][0]
    assert line.startswith('MODEL_PRICES="')  # entrecomillado por los espacios
    # round-trip: lo que lee Settings es exactamente lo que se mandó
    assert json.loads(envfile.read_env(p)["MODEL_PRICES"]) == {"m": {"input": 1}}
    assert not list(tmp_path.glob("*.tmp"))  # sin temporales colgados


def test_write_env_backup_failure_aborts_write(tmp_path, monkeypatch):
    p = _env(tmp_path)

    def boom(self, *args, **kwargs):
        raise OSError("disco lleno")

    monkeypatch.setattr(envfile.Path, "write_bytes", boom)
    with pytest.raises(RuntimeError):
        envfile.write_env(p, {"UPSTREAM_MODEL": "nuevo"})
    # el .env original queda intacto
    assert "UPSTREAM_MODEL=model-x" in p.read_text(encoding="utf-8")


def test_backup_rotation_keeps_ten(tmp_path):
    p = _env(tmp_path)
    for i in range(13):
        (tmp_path / f".env.bak-20260101-{i:06d}").write_text("old", encoding="utf-8")
    envfile.write_env(p, {"UPSTREAM_MODEL": "y"})
    backups = sorted(x.name for x in tmp_path.glob(".env.bak-*"))
    assert len(backups) == envfile.BACKUP_KEEP == 10
    assert ".env.bak-20260101-000000" not in backups  # los más antiguos rotaron


def test_reset_fields_writes_defaults_and_clears_none(tmp_path):
    p = _env(tmp_path, _SAMPLE + "EXPOSE_REASONING_CONTENT=true\n")
    envfile.reset_fields(
        p, ["LOG_LEVEL", "ATOMIC_FAST_PATH", "EXPOSE_REASONING_CONTENT"]
    )
    data = envfile.read_env(p)
    assert data["LOG_LEVEL"] == "INFO"           # default de Settings
    assert data["ATOMIC_FAST_PATH"] == "false"
    # default None = «sin definir» → la línea se borra
    assert "EXPOSE_REASONING_CONTENT" not in data
    with pytest.raises(KeyError):
        envfile.reset_fields(p, ["NO_EXISTE"])


# --- Paso 4: API de la UI (app/web/routes.py, §6) ---------------------------

@pytest.fixture
def ui_env(tmp_path, monkeypatch):
    """Apunta la API a un `.env` temporal: los tests nunca tocan el real."""
    path = tmp_path / ".env"
    path.write_text(_SAMPLE, encoding="utf-8")
    monkeypatch.setattr(routes, "ENV_PATH", path)
    registry.reset()
    return path


async def test_ui_catalog_endpoint_exposes_catalog_without_hardcoding(client, ui_env):
    resp = await client.get("/ui/api/catalog")
    assert resp.status_code == 200
    body = resp.json()
    assert body["groups"] == list(catalog.GROUPS)
    params = {p["field"]: p for p in body["params"]}
    assert set(params) == set(Settings.model_fields)
    assert all(p["widget"] in catalog.WIDGETS for p in body["params"])
    # los select llegan con sus opciones CERRADAS: el JS no puede inventarlas
    profile = params["atomic_profile"]
    assert profile["widget"] == "select"
    assert profile["options"] == ["fast", "balanced", "quality"]


async def test_get_config_groups_every_param_and_masks_secrets(client, ui_env, monkeypatch):
    monkeypatch.setattr(settings, "upstream_api_key", "sk-super-secreta-1234")
    resp = await client.get("/ui/api/config")
    assert resp.status_code == 200
    body = resp.json()
    assert [group["name"] for group in body["groups"]] == list(catalog.GROUPS)

    fields = [field for group in body["groups"] for field in group["fields"]]
    assert len(fields) == len(catalog.PARAMS)

    secret = next(f for f in fields if f["field"] == "upstream_api_key")
    assert secret["value"] == "\u2022\u2022\u2022\u20221234"
    assert "sk-super-secreta-1234" not in resp.text      # jamás en claro

    model = next(f for f in fields if f["field"] == "upstream_model")
    assert model["value"] == settings.upstream_model
    assert model["is_set"] is True                       # está en el .env de prueba


async def test_put_config_writes_env_reloads_and_backs_up(client, ui_env):
    resp = await client.put(
        "/ui/api/config", json={"atomic_profile": "fast", "trace_mode": "full"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is True
    assert body["changed"] == ["ATOMIC_PROFILE", "TRACE_MODE"]

    text = ui_env.read_text(encoding="utf-8")
    assert "ATOMIC_PROFILE=fast" in text
    assert "TRACE_MODE=full" in text
    # recarga en caliente: el proceso ve la config nueva sin reiniciar
    assert settings.atomic_profile == "fast"
    assert settings.effective_trace_mode() == "full"

    backups = list(ui_env.parent.glob(".env.bak-*"))
    assert len(backups) == 1
    assert "UPSTREAM_MODEL=model-x" in backups[0].read_text(encoding="utf-8")


async def test_put_invalid_value_leaves_env_byte_identical(client, ui_env):
    before = ui_env.read_bytes()
    resp = await client.put("/ui/api/config", json={"atomic_profile": "turbo"})
    assert resp.status_code == 422
    assert ui_env.read_bytes() == before
    assert not list(ui_env.parent.glob(".env.bak-*"))   # no se llegó a escribir


async def test_put_secrets_are_write_only_and_empty_keeps_current(client, ui_env):
    before = ui_env.read_bytes()

    masked = await client.put(
        "/ui/api/config", json={"upstream_api_key": "\u2022\u2022\u2022\u20221234"}
    )
    assert masked.status_code == 200
    assert masked.json()["applied"] is False       # la máscara no reescribe nada
    assert ui_env.read_bytes() == before

    empty = await client.put("/ui/api/config", json={"upstream_api_key": ""})
    assert empty.json()["applied"] is False        # vacío = conservar la actual
    assert ui_env.read_bytes() == before

    real = await client.put("/ui/api/config", json={"upstream_api_key": "sk-nueva-9999"})
    assert real.json()["applied"] is True
    assert "UPSTREAM_API_KEY=sk-nueva-9999" in ui_env.read_text(encoding="utf-8")
    assert settings.upstream_api_key == "sk-nueva-9999"


async def test_reset_endpoint_restores_catalog_defaults(client, ui_env):
    resp = await client.post(
        "/ui/api/config/reset", json={"fields": ["LOG_LEVEL", "EXPOSE_REASONING_CONTENT"]}
    )
    assert resp.status_code == 200
    data = envfile.read_env(ui_env)
    assert data["LOG_LEVEL"] == "INFO"
    assert "EXPOSE_REASONING_CONTENT" not in data   # default None → sin definir

    unknown = await client.post("/ui/api/config/reset", json={"fields": ["NO_EXISTE"]})
    assert unknown.status_code == 422


async def test_token_required_when_set(client, ui_env, monkeypatch):
    monkeypatch.setattr(settings, "ui_token", "t" * 16)

    # leer no exige token (y no filtra secretos: solo máscaras)
    assert (await client.get("/ui/api/config")).status_code == 200

    assert (await client.put("/ui/api/config", json={"atomic_profile": "fast"})).status_code == 401
    assert (await client.delete("/ui/api/sessions/x")).status_code == 401

    authorized = await client.put(
        "/ui/api/config", json={"atomic_profile": "fast"}, headers={"X-UI-Token": "t" * 16}
    )
    assert authorized.status_code == 200


async def test_sessions_endpoints(client, ui_env):
    listing = await client.get("/ui/api/sessions")
    assert listing.status_code == 200
    body = listing.json()
    assert body["active"] == 0
    assert body["sessions"] == []
    assert body["backend"] == settings.session_backend
    assert (await client.delete("/ui/api/sessions/no-existe")).status_code == 404


async def test_stats_endpoint_aggregates_registry(client, ui_env):
    registry.record(
        {
            "upstream_calls": 3,
            "retries": 1,
            "tool_calls": 0,
            "subtasks": 2,
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            "cost_usd": 0.25,
            "duration_seconds": 2.0,
            "limits_hit": ["max_total_tasks"],
            "phases": {"executor": {"calls": 3, "retries": 1}},
            "errors": [],
        }
    )
    body = (await client.get("/ui/api/stats")).json()
    assert body["totals"]["runs"] == 1
    assert body["totals"]["upstream_calls"] == 3
    assert body["totals"]["total_tokens"] == 150
    assert body["totals"]["cost_usd"] == 0.25
    assert body["by_phase"]["executor"] == {"calls": 3, "retries": 1}
    assert body["limits_hit"] == {"max_total_tasks": 1}
    assert body["config"]["model"] == settings.upstream_model
    assert len(body["recent"]) == 1


async def test_stats_stream_emits_named_event(ui_env):
    stream = routes.stats_events(interval=0.01)
    try:
        first = await anext(stream)
    finally:
        await stream.aclose()
    assert first.startswith("event: stats\ndata: {")
    assert '"totals"' in first


async def test_playground_streams_real_engine_events(client, ui_env, fake_upstream):
    fake_upstream.queue_completion(content='{"atomic": true, "subtasks": []}')
    fake_upstream.queue_stream(pieces=["hola desde el playground"])

    resp = await client.post(
        "/ui/api/playground",
        json={"messages": [{"role": "user", "content": "di hola"}], "profile": "fast"},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "event: chunk" in body
    assert "event: content" in body
    assert "hola desde el playground" in body
    assert "event: metrics" in body
    assert body.rstrip().endswith("data: [DONE]")



