"""Inference API: GET /health, GET /version, POST /predict, GET /metrics.

All configuration comes from environment variables:
  MODEL_PATH        path to model-<version>.joblib (metadata JSON next to it)
  API_VERSION       version of this API (set by the image build)
  APPLICATIONINSIGHTS_CONNECTION_STRING  enables Azure Monitor OpenTelemetry
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

import joblib
import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

API_VERSION = os.getenv("API_VERSION", "1.0.0")
MODEL_PATH = Path(os.getenv("MODEL_PATH", "/app/model/model-1.0.0.joblib"))
MAX_RECORDS = int(os.getenv("MAX_RECORDS", "1000"))

logger = logging.getLogger("ml-api")

# --- Telemetry -------------------------------------------------------------
if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(logger_name="ml-api")

from opentelemetry import metrics as otel_metrics  # noqa: E402

meter = otel_metrics.get_meter("ml-api")
inference_counter = meter.create_counter(
    "inferences", unit="1", description="Number of rows scored by the model"
)
latency_hist = meter.create_histogram(
    "model_latency_ms", unit="ms", description="Model inference latency per request"
)
error_counter = meter.create_counter(
    "inference_errors", unit="1", description="Failed /predict calls"
)

# --- Model loading ----------------------------------------------------------
_t0 = time.perf_counter()
model = joblib.load(MODEL_PATH)
meta = json.loads(MODEL_PATH.with_suffix(".json").read_text())
MODEL_LOAD_MS = round((time.perf_counter() - _t0) * 1000, 2)
FEATURES: list[str] = meta["features"]
MODEL_VERSION: str = meta["version"]

_stats_lock = threading.Lock()
_stats = {"calls": 0, "errors": 0, "rows": 0, "total_latency_ms": 0.0}

app = FastAPI(title="DSCC ML API", version=API_VERSION)


class PredictRequest(BaseModel):
    records: list[dict[str, float]] = Field(min_length=1, max_length=MAX_RECORDS)
    source: str | None = Field(default=None, description="Optional origin (blob name)")


class Prediction(BaseModel):
    label: int
    class_name: str
    probability: float


class PredictResponse(BaseModel):
    model_version: str
    api_version: str
    latency_ms: float
    predictions: list[Prediction]


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model_loaded": model is not None, "model_load_ms": MODEL_LOAD_MS}


@app.get("/version")
def version() -> dict:
    return {
        "api_version": API_VERSION,
        "model_version": MODEL_VERSION,
        "commit": os.getenv("GIT_SHA", "local"),
    }


@app.get("/metrics")
def get_metrics() -> dict:
    with _stats_lock:
        calls = _stats["calls"]
        return {
            "calls": calls,
            "errors": _stats["errors"],
            "rows_scored": _stats["rows"],
            "avg_latency_ms": round(_stats["total_latency_ms"] / calls, 3) if calls else 0.0,
        }


@app.post("/predict", response_model=PredictResponse)
def predict(req: PredictRequest) -> PredictResponse:
    missing = sorted({f for f in FEATURES if any(f not in r for r in req.records)})
    if missing:
        with _stats_lock:
            _stats["errors"] += 1
        error_counter.add(1, {"reason": "missing_features"})
        raise HTTPException(status_code=422, detail={"missing_features": missing[:10]})

    t0 = time.perf_counter()
    frame = np.asarray([[r[f] for f in FEATURES] for r in req.records], dtype=float)
    proba = model.predict_proba(frame)[:, 1]
    latency_ms = round((time.perf_counter() - t0) * 1000, 3)

    preds = [
        Prediction(
            label=int(p >= 0.5),
            class_name=meta["classes"][str(int(p >= 0.5))],
            probability=round(float(p), 4),
        )
        for p in proba
    ]
    with _stats_lock:
        _stats["calls"] += 1
        _stats["rows"] += len(preds)
        _stats["total_latency_ms"] += latency_ms
    attrs = {"model_version": MODEL_VERSION}
    inference_counter.add(len(preds), attrs)
    latency_hist.record(latency_ms, attrs)
    logger.info(
        "prediction",
        extra={"source": req.source, "rows": len(preds), "latency_ms": latency_ms},
    )
    return PredictResponse(
        model_version=MODEL_VERSION, api_version=API_VERSION, latency_ms=latency_ms, predictions=preds
    )
