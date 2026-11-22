import json
from datetime import UTC, datetime

import pytest
from shapely.geometry import Polygon

from viirs_slstr_hotspots.quality_report import QualityReport
from viirs_slstr_hotspots.store.schemas import (
    DetectionRecord,
    DetectionValidationResult,
    ValidationIssue,
)
from viirs_slstr_hotspots.store.validator import validate_detection_record


def valid_detection() -> dict[str, object]:
    return {
        "det_id": 1,
        "platform": "NOAA-20",
        "sensor_view": "VIIRS_I",
        "t_utc": datetime(2026, 11, 22, tzinfo=UTC),
        "lat": 55.75,
        "lon": 37.62,
        "footprint": Polygon(
            [(37.0, 55.0), (38.0, 55.0), (38.0, 56.0), (37.0, 56.0), (37.0, 55.0)]
        ).wkb,
        "granule": "granule-001",
        "line": 10,
        "sample": 20,
        "area_m2": 140625.0,
        "scan_angle": 0.0,
        "vza": 10.0,
        "sza": 35.0,
        "raa": 25.0,
        "glint_angle": 45.0,
        "stratum": "night_forest_nadir_VIIRS_I",
        "T_mir": 300.0,
        "T_tir": 290.0,
        "mir_channel": "I4",
        "r_block": 1.0,
        "r_win": 0.8,
        "s_win": 0.5,
        "h_lev": 0.2,
        "b_hist": 0.1,
        "q_hist": 0.6,
        "n_hist": 20,
        "s_eff": 0.7,
        "z": 4.1,
        "p_value": 0.0001,
        "nfa_pixel": 0.5,
        "nfa_cluster": 0.2,
        "nfa_multi": 0.1,
        "significance": 1.0,
        "decision": "confirmed",
        "frp_mw": 1.5,
        "frp_err_mw": 0.2,
        "frp_toa_mw": 1.7,
        "frp_50_mw": 0.8,
        "flags": 0,
        "event_id": None,
        "source_class": "unknown",
    }


def test_validate_detection_record_returns_validated_model():
    result = validate_detection_record(valid_detection())

    assert result.valid
    assert isinstance(result.record, DetectionRecord)
    assert result.record.det_id == 1
    assert result.record.event_id is None


@pytest.mark.parametrize("data", [{}, [], None])
def test_validate_detection_record_returns_invalid_result_for_empty_or_wrong_input(data):
    result = validate_detection_record(data)

    assert not result.valid
    assert result.record is None
    assert result.errors


def test_validate_detection_record_rejects_missing_required_field():
    data = valid_detection()
    del data["platform"]

    result = validate_detection_record(data)

    assert not result.valid
    assert any(
        issue.code == "MISSING_REQUIRED_FIELD" and issue.field == "platform"
        for issue in result.errors
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("platform", 20),
        ("det_id", "1"),
        ("flags", 2**32),
    ],
)
def test_validate_detection_record_returns_schema_validation_errors(field, value):
    result = validate_detection_record({**valid_detection(), field: value})

    assert not result.valid
    assert result.errors


def test_validate_detection_record_rejects_value_outside_declared_category():
    result = validate_detection_record({**valid_detection(), "source_class": "fire"})

    assert not result.valid
    assert any(
        issue.code == "INVALID_FIELD_VALUE" and issue.field == "source_class"
        for issue in result.errors
    )


def test_validate_detection_record_preserves_pydantic_error_details():
    result = validate_detection_record({"det_id": -1})

    issue = next(item for item in result.errors if item.field == "det_id")
    assert issue.type == "greater_than_equal"
    assert issue.code == "VALUE_OUT_OF_RANGE"
    assert issue.can_continue


def test_validator_results_are_aggregated_in_quality_report(tmp_path):
    report_path = tmp_path / "quality_report.json"
    report = QualityReport(report_path)

    valid_result = validate_detection_record(valid_detection(), quality_report=report)
    invalid_result = validate_detection_record(
        {**valid_detection(), "det_id": -1},
        quality_report=report,
    )
    report.flush()

    assert valid_result.valid
    assert not invalid_result.valid
    validation = json.loads(report_path.read_text(encoding="utf-8"))["validation"]
    assert validation["valid_count"] == 1
    assert validation["invalid_count"] == 1
    assert validation["errors"][0]["code"] == "VALUE_OUT_OF_RANGE"
    assert validation["errors"][0]["field"] == "det_id"
    assert "value" not in validation["errors"][0]


def test_quality_report_records_warning_without_invalidating_record(tmp_path):
    report_path = tmp_path / "quality_report.json"
    report = QualityReport(report_path)
    record = DetectionRecord.model_validate(valid_detection())
    warning = ValidationIssue(
        code="INVALID_FIELD_VALUE",
        name="Предупреждение",
        field="source_class",
        type="custom_warning",
        message="Проверка завершена с предупреждением.",
        severity="warning",
        can_continue=True,
    )
    report.record_validation_result(
        DetectionValidationResult(valid=True, record=record, warnings=[warning])
    )
    report.flush()

    validation = json.loads(report_path.read_text(encoding="utf-8"))["validation"]
    assert validation["valid_count"] == 1
    assert validation["invalid_count"] == 0
    assert validation["errors"] == []
    assert validation["warnings"][0]["severity"] == "warning"
