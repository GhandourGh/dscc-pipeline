import json
from pathlib import Path

import pytest

from shared.validation import (
    FEATURES,
    blob_name_from_subject,
    check_metadata,
    check_schema,
    idempotency_key,
    parse_records,
)

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "samples"


def test_schema_matches_model():
    model_meta = json.loads((ROOT / "model" / "model-1.0.0.json").read_text())
    assert FEATURES == model_meta["features"]


def test_subject_parsing():
    s = "/blobServices/default/containers/input/blobs/batch/day1.csv"
    assert blob_name_from_subject(s) == "batch/day1.csv"
    with pytest.raises(ValueError):
        blob_name_from_subject("/nope")


@pytest.mark.parametrize(
    "name,size,ok",
    [("a.csv", 100, True), ("A.CSV", 100, True), ("a.txt", 100, False),
     ("a.csv", 0, False), ("a.csv", 10_000_000, False)],
)
def test_metadata(name, size, ok):
    assert check_metadata(name, size, 1024 * 1024).ok is ok


def test_valid_file():
    r = check_schema((SAMPLES / "valid_batch.csv").read_bytes(), 1000)
    assert r.ok and r.rows == 10


def test_missing_column_rejected():
    r = check_schema((SAMPLES / "invalid_schema.csv").read_bytes(), 1000)
    assert not r.ok and "mean_radius" in r.reason


def test_poison_file_passes_dispatcher():
    # Structure is valid; the bad cell is only caught by the API (-> poison path).
    assert check_schema((SAMPLES / "poison_values.csv").read_bytes(), 1000).ok


def test_too_many_rows():
    assert not check_schema((SAMPLES / "valid_batch.csv").read_bytes(), 5).ok


def test_not_utf8_and_empty():
    assert not check_schema(b"\xff\xfe\x00", 10).ok
    assert not check_schema(b"", 10).ok


def test_parse_records():
    recs = parse_records((SAMPLES / "valid_batch.csv").read_bytes())
    assert len(recs) == 10 and set(recs[0]) == set(FEATURES)


def test_idempotency_key_stable():
    assert idempotency_key("a.csv", '"0x1"') == idempotency_key("a.csv", "0x1")
    assert idempotency_key("a.csv", "0x1") != idempotency_key("a.csv", "0x2")
