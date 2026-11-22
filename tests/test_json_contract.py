import json
import re
from pathlib import Path

from viirs_slstr_hotspots.store.schemas import (
    DetectionRecord,
    DetectionValidationResult,
    QualityReportData,
)


def _without_annotations(value):
    if isinstance(value, dict):
        return {
            key: _without_annotations(item)
            for key, item in value.items()
            if key not in {"title", "description"}
        }
    if isinstance(value, list):
        return [_without_annotations(item) for item in value]
    return value


def test_documented_json_schema_matches_pydantic_models():
    contract_path = (
        Path(__file__).resolve().parents[1] / "docs" / "contracts" / "JSON_CONTRACT.md"
    )
    contract = contract_path.read_text(encoding="utf-8")
    record_match = re.search(
        r"### Валидная запись.*?```json\n(.*?)\n```",
        contract,
        flags=re.DOTALL,
    )
    assert record_match is not None
    assert DetectionRecord.model_validate_json(record_match.group(1)).det_id == 42

    match = re.search(
        r"## 5\. JSON Schema.*?```json\n(.*?)\n```",
        contract,
        flags=re.DOTALL,
    )
    assert match is not None
    documented_schema = json.loads(match.group(1))

    models = {
        "DetectionRecord": DetectionRecord,
        "DetectionValidationResult": DetectionValidationResult,
        "QualityReportData": QualityReportData,
    }
    definitions = {}
    for name, model in models.items():
        model_schema = model.model_json_schema(mode="validation")
        definitions.update(model_schema.pop("$defs", {}))
        definitions[name] = model_schema

    expected_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "oneOf": [{"$ref": f"#/$defs/{name}"} for name in models],
        "$defs": _without_annotations(definitions),
    }

    assert documented_schema == expected_schema
