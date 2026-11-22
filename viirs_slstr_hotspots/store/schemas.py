"""Pydantic-схемы данных, передаваемых между хранилищем и обработкой."""

import base64
import binascii
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError
from shapely import wkb
from shapely.errors import GEOSException
from shapely.geometry.base import BaseGeometry

UInt32 = Annotated[int, Field(strict=True, ge=0, le=2**32 - 1)]
UInt64 = Annotated[int, Field(strict=True, ge=0, le=2**64 - 1)]
Int32 = Annotated[int, Field(strict=True, ge=-(2**31), le=2**31 - 1)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
FLOAT32_MAX = 3.4028234663852886e38
Float32 = Annotated[
    float,
    Field(
        strict=True,
        ge=-FLOAT32_MAX,
        le=FLOAT32_MAX,
        json_schema_extra={"format": "float"},
    ),
]
Float64 = Annotated[
    float,
    Field(
        strict=True,
        allow_inf_nan=False,
        json_schema_extra={"format": "double"},
    ),
]
Probability = Annotated[
    float,
    Field(
        strict=True,
        gt=0,
        le=1,
        json_schema_extra={"format": "float"},
    ),
]
UTC_TIMESTAMP_PATTERN = (
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,3}0{0,3})?(?:Z|\+00:00)$"
)
UTCDateTime = Annotated[
    datetime,
    Field(
        description="RFC 3339 timestamp in UTC, ending in Z or +00:00.",
        json_schema_extra={"pattern": UTC_TIMESTAMP_PATTERN},
    ),
]
WKB = Annotated[
    bytes,
    Field(
        strict=True,
        description="Polygon WKB bytes; JSON representation is base64.",
        json_schema_extra={"contentEncoding": "base64"},
    ),
]

ValidationCode = Literal[
    "INVALID_INPUT",
    "MISSING_REQUIRED_FIELD",
    "INVALID_FIELD_TYPE",
    "VALUE_OUT_OF_RANGE",
    "INVALID_FIELD_VALUE",
    "UNEXPECTED_FIELD",
]
ValidationSeverity = Literal["error", "warning"]
Platform = Literal["NOAA-20", "NOAA-21", "S-NPP", "S3A", "S3B", "S3C"]
SensorView = Literal["VIIRS_I", "VIIRS_M", "SLSTR_nadir", "SLSTR_oblique"]
MirChannel = Literal["I4", "M13", "S7", "F1"]
DetectionDecision = Literal["confirmed", "needs_confirmation", "rejected"]
SourceClass = Literal[
    "vegetation_fire",
    "persistent_industrial",
    "persistent_natural_candidate",
    "recurrent_seasonal",
    "volcano_candidate",
    "unknown",
]


class DetectionRecord(BaseModel):
    """Одна запись обнаружения согласно таблице 3.1 большого ТЗ.

    Геометрия хранится в модели в виде WKB bytes. Объекты Polygon, имеющие
    атрибуты ``geom_type == "Polygon"`` и ``wkb``, принимаются и переводятся в WKB.
    Измерения хранятся как Python float с диапазоном float32; запись GeoParquet
    должна сохранять их как float32, а координаты — как float64.
    """

    model_config = ConfigDict(extra="forbid")

    det_id: UInt64
    platform: Platform
    sensor_view: SensorView
    t_utc: UTCDateTime
    lat: Float64
    lon: Float64
    footprint: WKB
    granule: str
    line: Int32
    sample: Int32
    area_m2: Float32
    scan_angle: Float32
    vza: Float32
    sza: Float32
    raa: Float32
    glint_angle: Float32
    stratum: str
    T_mir: Float32
    T_tir: Float32
    mir_channel: MirChannel
    r_block: Float32
    r_win: Float32
    s_win: Float32
    h_lev: Float32
    b_hist: Float32 | None = None
    q_hist: Float32 | None = None
    n_hist: NonNegativeInt
    s_eff: Float32
    z: Float32
    p_value: Probability
    nfa_pixel: Float32
    nfa_cluster: Float32
    nfa_multi: Float32
    significance: Float32
    decision: DetectionDecision
    frp_mw: Float32
    frp_err_mw: Float32
    frp_toa_mw: Float32
    frp_50_mw: Float32
    flags: UInt32
    event_id: str | None = None
    source_class: SourceClass

    @field_validator("t_utc")
    @classmethod
    def require_utc_datetime(cls, value: datetime) -> datetime:
        """Принимает время UTC с точностью не грубее миллисекунд."""
        if value.utcoffset() != timedelta(0):
            raise ValueError("t_utc должен содержать время в UTC.")
        if value.microsecond % 1000:
            raise ValueError("t_utc должен иметь точность до миллисекунд.")
        return value.astimezone(UTC)

    @field_validator("t_utc", mode="before")
    @classmethod
    def require_utc_json_timestamp(cls, value: object) -> object:
        """Ограничивает строковое представление временем RFC 3339 в UTC."""
        if isinstance(value, str):
            if re.fullmatch(UTC_TIMESTAMP_PATTERN, value) is None:
                raise ValueError("t_utc должен быть строкой RFC 3339 в UTC.")
        elif not isinstance(value, datetime):
            raise PydanticCustomError(
                "datetime_type",
                "t_utc должен быть объектом datetime или строкой RFC 3339 в UTC.",
            )
        return value

    @field_validator("footprint", mode="before")
    @classmethod
    def polygon_to_wkb(cls, value: object) -> object:
        """Проверяет WKB и переводит Polygon или base64 в байты WKB."""
        if isinstance(value, str):
            try:
                value = base64.b64decode(value, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("footprint должен быть корректной строкой base64.") from exc

        if isinstance(value, BaseGeometry):
            if value.geom_type != "Polygon":
                raise ValueError("footprint должен иметь геометрию типа Polygon.")
            value = value.wkb

        if not isinstance(value, bytes):
            raise ValueError("footprint должен быть Polygon или байтами WKB.")

        try:
            geometry = wkb.loads(value)
        except (GEOSException, ValueError, TypeError) as exc:
            raise ValueError("footprint содержит некорректный WKB.") from exc
        if geometry.geom_type != "Polygon":
            raise ValueError("footprint должен содержать геометрию типа Polygon.")
        return value

    @model_validator(mode="after")
    def require_history_values_when_available(self) -> "DetectionRecord":
        """Согласует nullable-статистики истории со счётчиком наблюдений."""
        history_values_missing = self.b_hist is None and self.q_hist is None
        if self.n_hist == 0 and not history_values_missing:
            raise ValueError("b_hist и q_hist должны быть null при n_hist == 0.")
        if self.n_hist > 0 and (self.b_hist is None or self.q_hist is None):
            raise ValueError("b_hist и q_hist обязательны при n_hist > 0.")
        return self

    @field_serializer("footprint", when_used="json")
    def serialize_footprint(self, value: bytes) -> str:
        """Кодирует WKB в base64 для JSON-представления."""
        return base64.b64encode(value).decode("ascii")


class ValidationIssue(BaseModel):
    """Одна ошибка или предупреждение валидации."""

    model_config = ConfigDict(extra="forbid")

    code: ValidationCode
    name: str
    field: str | None
    type: str
    message: str
    severity: ValidationSeverity
    can_continue: bool


class DetectionValidationResult(BaseModel):
    """Результат проверки одной записи обнаружения."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    record: DetectionRecord | None = None
    errors: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_consistent_result(self) -> "DetectionValidationResult":
        """Согласует статус записи с моделью и перечнем ошибок."""
        if self.valid != (self.record is not None):
            raise ValueError("record должен быть задан только для валидного результата.")
        if self.valid and self.errors:
            raise ValueError("Валидный результат не должен содержать ошибок.")
        if not self.valid and not self.errors:
            raise ValueError("Невалидный результат должен содержать хотя бы одну ошибку.")
        return self


class ValidationSummary(BaseModel):
    """Сводка результатов валидации для отчёта одного запуска."""

    model_config = ConfigDict(extra="forbid")

    valid_count: int = Field(strict=True, ge=0)
    invalid_count: int = Field(strict=True, ge=0)
    errors: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)


class QualityReportData(BaseModel):
    """JSON-отчёт с обязательной секцией валидации и расширяемыми данными."""

    model_config = ConfigDict(extra="allow")

    validation: ValidationSummary
