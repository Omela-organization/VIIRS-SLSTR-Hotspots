from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from shapely.geometry import Polygon

from viirs_slstr_hotspots.store.schemas import DetectionRecord


def valid_detection(**overrides):
    values = {
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
    values.update(overrides)
    return values


def test_detection_record_accepts_contract_fields_and_nullable_event_id():
    record = DetectionRecord.model_validate(valid_detection())

    assert record.det_id == 1
    assert record.t_utc.tzinfo == UTC
    assert record.event_id is None


def test_detection_record_accepts_polygon_and_stores_its_wkb():
    polygon = Polygon(
        [(37.0, 55.0), (38.0, 55.0), (38.0, 56.0), (37.0, 56.0), (37.0, 55.0)]
    )

    record = DetectionRecord.model_validate(valid_detection(footprint=polygon))

    assert record.footprint == polygon.wkb


def test_detection_record_json_round_trip_encodes_wkb_as_base64():
    record = DetectionRecord.model_validate(valid_detection())

    encoded = record.model_dump_json()
    decoded = DetectionRecord.model_validate_json(encoded)

    assert decoded.footprint == record.footprint
    assert decoded.t_utc == record.t_utc


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("det_id", -1),
        ("det_id", 2**64),
        ("flags", -1),
        ("flags", 2**32),
        ("line", 2**31),
        ("sample", -(2**31) - 1),
        ("lat", "not-a-number"),
        ("frp_mw", "1.5"),
        ("frp_mw", 3.402823466385289e38),
        ("frp_mw", float("nan")),
        ("frp_mw", float("inf")),
        ("p_value", 0.0),
        ("p_value", 1.01),
        ("platform", 20),
        ("platform", "unknown"),
        ("sensor_view", "VIIRS"),
        ("mir_channel", "I5"),
        ("decision", "maybe"),
        ("source_class", "fire"),
        ("footprint", "not-wkb"),
        ("footprint", b"invalid-wkb"),
    ],
)
def test_detection_record_rejects_invalid_types_and_ranges(field, value):
    with pytest.raises(ValidationError):
        DetectionRecord.model_validate(valid_detection(**{field: value}))


@pytest.mark.parametrize(
    "timestamp",
    [
        datetime(2026, 11, 22),
        datetime.fromisoformat("2026-11-22T03:00:00+03:00"),
    ],
)
def test_detection_record_rejects_non_utc_timestamps(timestamp):
    with pytest.raises(ValidationError, match="в UTC"):
        DetectionRecord.model_validate(valid_detection(t_utc=timestamp))


def test_detection_record_requires_millisecond_timestamp_precision():
    timestamp = datetime(2026, 11, 22, 0, 0, 0, 123456, tzinfo=UTC)

    with pytest.raises(ValidationError, match="миллисекунд"):
        DetectionRecord.model_validate(valid_detection(t_utc=timestamp))


def test_detection_record_accepts_millisecond_timestamp_precision():
    record = DetectionRecord.model_validate_json(
        DetectionRecord.model_validate(
            valid_detection(
                t_utc=datetime(2026, 11, 22, 0, 0, 0, 123000, tzinfo=UTC)
            )
        ).model_dump_json()
    )

    assert record.t_utc.microsecond == 123000


def test_history_statistics_are_nullable_only_without_history():
    record = DetectionRecord.model_validate(
        valid_detection(b_hist=None, q_hist=None, n_hist=0)
    )

    assert record.b_hist is None
    assert record.q_hist is None

    with pytest.raises(ValidationError, match="обязательны при n_hist > 0"):
        DetectionRecord.model_validate(valid_detection(q_hist=None))

    with pytest.raises(ValidationError, match="должны быть null при n_hist == 0"):
        DetectionRecord.model_validate(valid_detection(n_hist=0))


def test_detection_record_requires_non_nullable_contract_fields():
    values = valid_detection()
    del values["platform"]

    with pytest.raises(ValidationError):
        DetectionRecord.model_validate(values)


def test_detection_record_accepts_omitted_nullable_event_id():
    values = valid_detection()
    del values["event_id"]

    record = DetectionRecord.model_validate(values)

    assert record.event_id is None


def test_detection_record_rejects_unknown_fields():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        DetectionRecord.model_validate(valid_detection(unexpected="value"))
