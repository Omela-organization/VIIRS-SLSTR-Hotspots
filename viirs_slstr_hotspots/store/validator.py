"""Проверка данных по схемам хранилища."""

from typing import Any

from pydantic import ValidationError

from ..quality_report import QualityReport
from .schemas import (
    DetectionRecord,
    DetectionValidationResult,
    ValidationCode,
    ValidationIssue,
)

_ERROR_NAMES = {
    "INVALID_INPUT": "Некорректная запись",
    "MISSING_REQUIRED_FIELD": "Отсутствует обязательное поле",
    "INVALID_FIELD_TYPE": "Некорректный тип поля",
    "VALUE_OUT_OF_RANGE": "Значение вне допустимого диапазона",
    "INVALID_FIELD_VALUE": "Недопустимое значение поля",
    "UNEXPECTED_FIELD": "Неизвестное поле",
}


def _error_code(error_type: str) -> ValidationCode:
    if error_type in {"model_type", "model_attributes_type"}:
        return "INVALID_INPUT"
    if error_type == "missing":
        return "MISSING_REQUIRED_FIELD"
    if error_type == "extra_forbidden":
        return "UNEXPECTED_FIELD"
    if error_type in {
        "greater_than",
        "greater_than_equal",
        "less_than",
        "less_than_equal",
        "finite_number",
    }:
        return "VALUE_OUT_OF_RANGE"
    if error_type.endswith(("_type", "_parsing")):
        return "INVALID_FIELD_TYPE"
    return "INVALID_FIELD_VALUE"


def _to_validation_issue(error: dict[str, Any]) -> ValidationIssue:
    code = _error_code(error["type"])
    location = error["loc"]
    field = ".".join(str(part) for part in location) if location else None
    return ValidationIssue(
        code=code,
        name=_ERROR_NAMES[code],
        field=field,
        type=error["type"],
        message=error["msg"],
        severity="error",
        can_continue=True,
    )


def validate_detection_record(
    data: object,
    *,
    quality_report: QualityReport | None = None,
) -> DetectionValidationResult:
    """Проверяет одну запись и возвращает её модель либо ошибки проверки.

    Некорректная запись не останавливает вызывающий процесс: она возвращается
    как результат с ``valid=False``. Если передан отчёт качества, результат
    также учитывается в его сводке.
    """
    try:
        record = DetectionRecord.model_validate(data)
    except ValidationError as exc:
        result = DetectionValidationResult(
            valid=False,
            errors=[
                _to_validation_issue(error)
                for error in exc.errors(include_input=False, include_context=False)
            ],
        )
    else:
        result = DetectionValidationResult(valid=True, record=record)

    if quality_report is not None:
        quality_report.record_validation_result(result)

    return result
