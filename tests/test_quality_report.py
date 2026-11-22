import json
import logging

import pytest

from viirs_slstr_hotspots.quality_report import QualityReport


def test_flush_writes_report_as_utf8_json_and_creates_parent_directory(tmp_path):
    report_path = tmp_path / "nested" / "quality_report.json"
    report = QualityReport(report_path)
    report.set("спутник", "NOAA-20")
    report.set("counts", {"всего": 10, "годных": 8})

    report.flush()

    assert json.loads(report_path.read_text(encoding="utf-8")) == {
        "спутник": "NOAA-20",
        "counts": {"всего": 10, "годных": 8},
        "validation": {
            "valid_count": 0,
            "invalid_count": 0,
            "errors": [],
            "warnings": [],
        },
    }


def test_set_replaces_value_for_existing_key(tmp_path):
    report_path = tmp_path / "quality_report.json"
    report = QualityReport(report_path)
    report.set("count", 1)
    report.set("count", 2)

    report.flush()

    assert json.loads(report_path.read_text(encoding="utf-8")) == {
        "count": 2,
        "validation": {
            "valid_count": 0,
            "invalid_count": 0,
            "errors": [],
            "warnings": [],
        },
    }


def test_set_rejects_reserved_validation_key(tmp_path):
    report = QualityReport(tmp_path / "quality_report.json")

    with pytest.raises(ValueError, match="зарезервирован"):
        report.set("validation", {})


def test_set_rejects_non_string_key(tmp_path):
    report = QualityReport(tmp_path / "quality_report.json")

    with pytest.raises(TypeError, match="Ключ отчёта качества должен быть строкой"):
        report.set(1, "value")  # type: ignore[arg-type]


def test_flush_rejects_non_json_value_without_truncating_existing_report(tmp_path):
    report_path = tmp_path / "quality_report.json"
    report_path.write_text('{"existing": true}\n', encoding="utf-8")
    report = QualityReport(report_path)
    report.set("unsupported", object())

    with pytest.raises(ValueError, match="нельзя представить в JSON"):
        report.flush()

    assert json.loads(report_path.read_text(encoding="utf-8")) == {"existing": True}


def test_flush_surfaces_and_logs_file_write_error(tmp_path, caplog):
    report_path = tmp_path / "quality_report.json"
    report_path.mkdir()
    report = QualityReport(report_path)

    with (
        caplog.at_level(logging.ERROR, logger="viirs_slstr_hotspots.quality_report"),
        pytest.raises(OSError, match="Не удалось записать отчёт качества"),
    ):
        report.flush()

    assert "Не удалось записать отчёт качества" in caplog.text


def test_flush_preserves_existing_report_if_atomic_replace_fails(
    tmp_path, monkeypatch, caplog
):
    report_path = tmp_path / "quality_report.json"
    report_path.write_text('{"existing": true}\n', encoding="utf-8")
    report = QualityReport(report_path)
    report.set("updated", True)

    def fail_replace(source, destination):
        raise PermissionError("write denied")

    monkeypatch.setattr("viirs_slstr_hotspots.quality_report.os.replace", fail_replace)

    with (
        caplog.at_level(logging.ERROR, logger="viirs_slstr_hotspots.quality_report"),
        pytest.raises(OSError, match="Не удалось записать отчёт качества"),
    ):
        report.flush()

    assert json.loads(report_path.read_text(encoding="utf-8")) == {"existing": True}
    assert list(tmp_path.iterdir()) == [report_path]
