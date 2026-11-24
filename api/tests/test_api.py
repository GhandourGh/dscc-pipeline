import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("MODEL_PATH", str(ROOT / "model" / "model-1.0.0.joblib"))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import FEATURES, app  # noqa: E402

client = TestClient(app)


@pytest.fixture
def record() -> dict:
    import csv

    with open(ROOT / "samples" / "valid_batch.csv") as fh:
        row = next(csv.DictReader(fh))
    return {k: float(v) for k, v in row.items()}


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_version():
    r = client.get("/version")
    assert r.status_code == 200
    assert r.json()["model_version"] == "1.0.0"


def test_predict_success(record):
    r = client.post("/predict", json={"records": [record, record]})
    assert r.status_code == 200
    body = r.json()
    assert len(body["predictions"]) == 2
    assert 0.0 <= body["predictions"][0]["probability"] <= 1.0
    assert body["predictions"][0]["class_name"] in {"benign", "malignant"}


def test_predict_invalid_value(record):
    record[FEATURES[0]] = "not-a-number"
    r = client.post("/predict", json={"records": [record]})
    assert r.status_code == 422


def test_predict_missing_feature(record):
    record.pop(FEATURES[0])
    r = client.post("/predict", json={"records": [record]})
    assert r.status_code == 422
    assert FEATURES[0] in json.dumps(r.json())


def test_predict_missing_body():
    r = client.post("/predict")
    assert r.status_code == 422


def test_predict_empty_records():
    r = client.post("/predict", json={"records": []})
    assert r.status_code == 422


def test_metrics_counts_calls(record):
    before = client.get("/metrics").json()["calls"]
    client.post("/predict", json={"records": [record]})
    after = client.get("/metrics").json()
    assert after["calls"] == before + 1
    assert after["avg_latency_ms"] >= 0
