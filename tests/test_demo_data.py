"""The demo data generator has to hold to the same bar the platform holds it to.

`validate` below is a faithful port of the server's structural validation
(`ingest/validation.py`): required, type, enum, format, range. Every clean record the
generator emits has to pass it, and every deliberately defective one has to fail it —
otherwise the demo data lies about what it is exercising.
"""

from __future__ import annotations

import importlib.util
import random
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "examples" / "demo_data.py"
_spec = importlib.util.spec_from_file_location("demo_data", _MODULE_PATH)
assert _spec and _spec.loader
demo_data = importlib.util.module_from_spec(_spec)
sys.modules["demo_data"] = demo_data  # dataclasses resolves annotations through sys.modules
_spec.loader.exec_module(demo_data)


# --- the server's structural validation, ported -----------------------------------------


def _type_ok(attribute: dict[str, Any], value: Any) -> bool:
    match attribute["type"]:
        case "string":
            return isinstance(value, str)
        case "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        case "boolean":
            return isinstance(value, bool)
        case "date" | "datetime":
            if not isinstance(value, str):
                return False
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return False
            return True
        case "enum":
            return value in (attribute.get("enum_values") or [])
        case _:
            return True


def validate(definition: dict[str, Any], record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for attribute in definition["attributes"]:
        value = record.get(attribute["name"])
        if value in (None, ""):
            if attribute.get("required"):
                errors.append(f"'{attribute['name']}' is required")
            continue
        if not _type_ok(attribute, value):
            errors.append(f"'{attribute['name']}' must be {attribute['type']}")
            continue
        pattern = attribute.get("regex")
        if pattern and isinstance(value, str) and not re.search(pattern, value):
            errors.append(f"'{attribute['name']}' does not match the required format")
        if attribute["type"] == "number":
            low, high = attribute.get("min_value"), attribute.get("max_value")
            if low is not None and value < low:
                errors.append(f"'{attribute['name']}' is below {low}")
            if high is not None and value > high:
                errors.append(f"'{attribute['name']}' exceeds {high}")
    return errors


def conform(record: dict[str, Any], spec: Any) -> dict[str, Any]:
    """What Masterly does at the door: rename source fields to model attributes."""
    if not spec.field_map:
        return dict(record)
    return {spec.field_map.get(k, k): v for k, v in record.items()}


ALL_MODELS = sorted(demo_data.MODEL_SPECS)


def build(model_key: str, count: int = 40, *, seed: int = 7, defect_rate: float = 0.0) -> list[Any]:
    return demo_data.build_batches(
        model_key,
        count,
        random.Random(seed),
        duplicate_rate=0.2,
        defect_rate=defect_rate,
    )


# --- tests ------------------------------------------------------------------------------


@pytest.mark.parametrize("model_key", ALL_MODELS)
def test_clean_records_pass_the_models_own_constraints(model_key: str) -> None:
    definition = demo_data.MODEL_SPECS[model_key].definition
    for batch in build(model_key):
        assert batch.records, f"{batch.spec.name} delivered nothing"
        for record in batch.records:
            errors = validate(definition, conform(record, batch.spec))
            assert not errors, f"{batch.spec.name}: {errors} in {record}"


@pytest.mark.parametrize("model_key", ALL_MODELS)
def test_every_defect_actually_breaks_something(model_key: str) -> None:
    """A defect must either violate a constraint or remove the natural key — the two ways
    a record legitimately lands in quarantine."""
    model_spec = demo_data.MODEL_SPECS[model_key]
    entity = demo_data.ENTITY_FACTORIES[model_key](random.Random(1), 1)[0]
    for source in model_spec.sources:
        rendered = demo_data.render(entity, source, "KEY-1", random.Random(2))
        for defect in model_spec.defects:
            if defect.attribute in source.drops:
                continue
            broken = demo_data.apply_defect(rendered, defect)
            key_attribute = source.key_attribute
            if not broken.get(key_attribute):
                continue  # quarantines as "missing source key"
            assert validate(model_spec.definition, broken), (
                f"defect '{defect.label}' on {source.name} passes validation"
            )


@pytest.mark.parametrize("model_key", ALL_MODELS)
def test_records_speak_the_delivering_systems_field_names(model_key: str) -> None:
    for batch in build(model_key):
        known = set(batch.spec.field_map) or {
            a["name"] for a in demo_data.MODEL_SPECS[model_key].definition["attributes"]
        }
        for record in batch.records:
            assert set(record) <= known, f"{batch.spec.name} leaked {set(record) - known}"


@pytest.mark.parametrize("model_key", ALL_MODELS)
def test_source_keys_are_present_and_unique_within_a_source(model_key: str) -> None:
    for batch in build(model_key):
        conformed = [conform(r, batch.spec) for r in batch.records]
        keys = [r.get(batch.spec.key_attribute) for r in conformed]
        assert all(keys), f"{batch.spec.name} delivered a record with no key"
        assert len(set(keys)) == len(keys), f"{batch.spec.name} repeated a source key"


def test_the_same_entity_reaches_several_systems_under_different_keys() -> None:
    """The premise of the whole demo: identity resolution has something to resolve."""
    batches = build("customer", count=60)
    by_source = {b.spec.name: b for b in batches}
    assert len(by_source) >= 3
    crm_keys = {conform(r, by_source["crm"].spec)["customer_number"]
                for r in by_source["crm"].records}
    erp_keys = {conform(r, by_source["erp"].spec)["customer_number"]
                for r in by_source["erp"].records}
    assert not crm_keys & erp_keys, "systems must not share a key space"
    assert len(by_source["erp"].records) > 20, "the ERP should carry most of the population"
    assert sum(b.duplicates for b in batches) > 0, "no intra-source duplicates generated"


@pytest.mark.parametrize("model_key", ALL_MODELS)
def test_generation_is_reproducible(model_key: str) -> None:
    first = [b.records for b in build(model_key, defect_rate=0.1)]
    second = [b.records for b in build(model_key, defect_rate=0.1)]
    assert first == second
    different = [b.records for b in build(model_key, seed=99, defect_rate=0.1)]
    assert first != different


def test_defect_rate_is_honoured() -> None:
    batches = build("customer", count=200, defect_rate=0.25)
    total = sum(len(b.records) for b in batches)
    defective = sum(b.defective for b in batches)
    assert 0.15 < defective / total < 0.30


@pytest.mark.parametrize("model_key", ALL_MODELS)
def test_adaptive_mode_generates_valid_records_for_a_live_definition(model_key: str) -> None:
    """Adaptive mode reads a model it has never seen. Fed one of our own definitions, the
    records it invents must still satisfy that model."""
    spec = demo_data.MODEL_SPECS[model_key]
    key_attributes = spec.definition["keys"][0]["attributes"]
    records, warnings = demo_data.build_adaptive_records(
        spec.definition, key_attributes, 25, random.Random(3), defect_rate=0.0
    )
    assert len(records) == 25
    for record in records:
        assert validate(spec.definition, record) == [], warnings
    for record in records:
        for key_attribute in key_attributes:
            assert record[key_attribute]


def test_adaptive_mode_warns_about_formats_it_cannot_satisfy() -> None:
    definition = {
        "attributes": [
            {"name": "code", "type": "string", "required": True},
            {"name": "checksum", "type": "string", "regex": r"^\d{18}$"},
        ],
        "keys": [{"name": "code", "attributes": ["code"]}],
    }
    records, warnings = demo_data.build_adaptive_records(
        definition, ["code"], 5, random.Random(4), defect_rate=0.0
    )
    assert any("checksum" in w for w in warnings)
    assert all("checksum" not in r for r in records), "an unsatisfiable format is left empty"


def test_dry_run_writes_records_without_touching_an_install(tmp_path: Path) -> None:
    out = tmp_path / "records.json"
    exit_code = demo_data.main(
        ["--dry-run", "--models", "customer", "--customers", "5", "--out", str(out)]
    )
    assert exit_code == 0
    payload = __import__("json").loads(out.read_text(encoding="utf-8"))
    assert set(payload) == {"crm", "erp", "webshop"}
    assert sum(len(v) for v in payload.values()) > 5


@pytest.mark.parametrize("model_key", ALL_MODELS)
def test_a_model_this_script_created_is_recognised_as_its_own(model_key: str) -> None:
    spec = demo_data.MODEL_SPECS[model_key]
    demo_data.check_model_is_ours({"definition": spec.definition}, spec)  # must not raise


def test_a_foreign_model_under_the_same_name_is_refused() -> None:
    """The trap this guard exists for: a leftover `Customer` from another seed is found by
    exactly the same name lookup, and adopting it quarantines the whole batch."""
    spec = demo_data.MODEL_SPECS["customer"]
    foreign = {
        "definition": {
            "attributes": [
                {"name": "ext_id", "type": "string", "required": True},
                {"name": "name", "type": "string", "required": True},
            ]
        }
    }
    with pytest.raises(SystemExit) as refused:
        demo_data.check_model_is_ours(foreign, spec)
    message = str(refused.value)
    assert "already exists" in message
    assert "ext_id" in message  # names what it demands that we do not generate
    assert "customer_number" in message  # and what it lacks that we do generate


def test_a_model_that_merely_has_extra_attributes_is_still_ours() -> None:
    """Someone adding a column to our model is not a reason to refuse — we still fill every
    attribute it requires."""
    spec = demo_data.MODEL_SPECS["product"]
    widened = {
        "definition": {
            "attributes": [
                *spec.definition["attributes"],
                {"name": "shelf_life_days", "type": "number"},
            ]
        }
    }
    demo_data.check_model_is_ours(widened, spec)


@pytest.mark.parametrize("model_key", ALL_MODELS)
def test_the_business_key_is_never_a_per_system_number(model_key: str) -> None:
    """The bug this guards: keying a model on the number each system assigns mints one
    entity per system, and deterministic resolution has nothing left to resolve. A business
    key has to be an identifier the systems SHARE."""
    spec = demo_data.MODEL_SPECS[model_key]
    per_system = {source.key_attribute for source in spec.sources}
    for key in spec.definition["keys"]:
        assert not (set(key["attributes"]) & per_system), (
            f"{spec.name} keys on {key['attributes']}, which is a source's own key"
        )


@pytest.mark.parametrize("model_key", ALL_MODELS)
def test_the_business_key_is_carried_by_at_least_two_systems(model_key: str) -> None:
    """A shared key only resolves across systems if more than one system delivers it."""
    spec = demo_data.MODEL_SPECS[model_key]
    for key in spec.definition["keys"]:
        for attribute in key["attributes"]:
            carriers = [s.name for s in spec.sources if attribute not in s.drops]
            assert len(carriers) >= 2, f"{spec.name}: only {carriers} carry '{attribute}'"


@pytest.mark.parametrize("model_key", ALL_MODELS)
def test_match_config_compares_attributes_the_model_actually_has(model_key: str) -> None:
    spec = demo_data.MODEL_SPECS[model_key]
    attributes = {a["name"] for a in spec.definition["attributes"]}
    assert spec.match, f"{spec.name} has no match config — key-less records would not resolve"
    assert set(spec.match["attributes"]) <= attributes
    assert spec.match["blocking"]["attribute"] in attributes
    assert spec.match["review_threshold"] <= spec.match["auto_threshold"]


def test_a_system_without_the_business_key_still_reaches_matching() -> None:
    """The webshop is the reason the match config exists: it carries no registration number,
    so its records arrive unlinked and only probabilistic matching can place them."""
    spec = demo_data.MODEL_SPECS["customer"]
    webshop = next(s for s in spec.sources if s.name == "webshop")
    assert "org_number" in webshop.drops
    assert set(spec.match["attributes"]) - set(webshop.drops), "webshop carries nothing to match on"
