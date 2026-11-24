"""One Flex Consumption Function App (Python 3.12, v2 model) with 3 functions.

  dispatcher  Event Grid trigger  BlobCreated on input/ -> validate -> queue or rejected/
  worker      Queue trigger       inference-requests -> API /predict -> output/ + Cosmos DB
  recent      HTTP trigger        GET /api/recent -> last 20 inferences (JSON)

Application settings (no secrets, identity-based connections):
  AzureWebJobsStorage__accountName   storage account (blob + queue, managed identity)
  STORAGE_ACCOUNT_NAME               same account, used by the SDK clients
  COSMOS_ENDPOINT                    https://<account>.documents.azure.com:443/
  COSMOS_DATABASE / COSMOS_CONTAINER pipeline / inferences
  API_URL                            https://<api>.<env>.<region>.azurecontainerapps.io
  MAX_FILE_BYTES / MAX_ROWS / API_TIMEOUT_S   validation and HTTP limits
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime

import azure.functions as func
import httpx

from shared.validation import (
    blob_name_from_subject,
    check_metadata,
    check_schema,
    idempotency_key,
    parse_records,
)

# --- Telemetry (Azure Monitor OpenTelemetry distro) ------------------------
if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING") and os.getenv("PYTHON_ENABLE_OPENTELEMETRY"):
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor()

from opentelemetry import metrics as otel_metrics  # noqa: E402

_meter = otel_metrics.get_meter("pipeline-functions")
files_counter = _meter.create_counter("pipeline_files", description="Files by outcome")
e2e_latency = _meter.create_histogram(
    "pipeline_e2e_latency_ms", unit="ms", description="Upload -> result stored"
)
api_latency = _meter.create_histogram(
    "pipeline_api_latency_ms", unit="ms", description="Worker -> API /predict round trip"
)

log = logging.getLogger("pipeline")

QUEUE = "inference-requests"
INPUT, OUTPUT, REJECTED = "input", "output", "rejected"
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_BYTES", str(1024 * 1024)))
MAX_ROWS = int(os.getenv("MAX_ROWS", "1000"))
API_TIMEOUT_S = float(os.getenv("API_TIMEOUT_S", "10"))

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# --- Lazily created, reused clients (one per worker process) ---------------
_blob_service = None
_cosmos_container = None
_http: httpx.Client | None = None


def blob_service():
    global _blob_service
    if _blob_service is None:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        account = os.environ["STORAGE_ACCOUNT_NAME"]
        _blob_service = BlobServiceClient(
            f"https://{account}.blob.core.windows.net", credential=DefaultAzureCredential()
        )
    return _blob_service


def cosmos():
    global _cosmos_container
    if _cosmos_container is None:
        from azure.cosmos import CosmosClient
        from azure.identity import DefaultAzureCredential

        client = CosmosClient(os.environ["COSMOS_ENDPOINT"], credential=DefaultAzureCredential())
        _cosmos_container = client.get_database_client(
            os.getenv("COSMOS_DATABASE", "pipeline")
        ).get_container_client(os.getenv("COSMOS_CONTAINER", "inferences"))
    return _cosmos_container


def http() -> httpx.Client:
    global _http
    if _http is None:
        _http = httpx.Client(
            base_url=os.environ["API_URL"],
            timeout=httpx.Timeout(API_TIMEOUT_S, connect=5.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _http


def _reject(name: str, reason: str, content: bytes | None) -> None:
    from azure.storage.blob import ContentSettings

    svc = blob_service()
    target = svc.get_blob_client(REJECTED, name)
    meta = {"reason": reason[:250], "rejected_at": datetime.now(UTC).isoformat()}
    if content is None:
        source = svc.get_blob_client(INPUT, name)
        target.start_copy_from_url(source.url, metadata=meta)
    else:
        target.upload_blob(content, overwrite=True, metadata=meta)
    svc.get_blob_client(REJECTED, f"{name}.reason.json").upload_blob(
        json.dumps({"file": name, **meta}),
        overwrite=True,
        content_settings=ContentSettings(content_type="application/json"),
    )


# ---------------------------------------------------------------------------
# 1. dispatcher: Event Grid -> validate -> queue | rejected/
# ---------------------------------------------------------------------------
@app.function_name("dispatcher")
@app.event_grid_trigger(arg_name="event")
@app.queue_output(arg_name="msg", queue_name=QUEUE, connection="AzureWebJobsStorage")
def dispatcher(event: func.EventGridEvent, msg: func.Out[str]) -> None:
    data = event.get_json()
    name = blob_name_from_subject(event.subject)
    size = int(data.get("contentLength", 0))
    etag = data.get("eTag", "")
    log.info("blob_created", extra={"file": name, "size": size, "event_id": event.id})

    check = check_metadata(name, size, MAX_FILE_BYTES)
    content = None
    if check.ok:
        content = blob_service().get_blob_client(INPUT, name).download_blob().readall()
        check = check_schema(content, MAX_ROWS)

    if not check.ok:
        _reject(name, check.reason, content)
        files_counter.add(1, {"outcome": "rejected"})
        log.warning("file_rejected", extra={"file": name, "reason": check.reason})
        return

    message = {
        "blobName": name,
        "eTag": etag,
        "size": size,
        "rows": check.rows,
        "eventTime": event.event_time.isoformat() if event.event_time else None,
        "enqueuedAt": datetime.now(UTC).isoformat(),
    }
    msg.set(json.dumps(message))
    files_counter.add(1, {"outcome": "queued"})
    log.info("file_queued", extra=message)


# ---------------------------------------------------------------------------
# 2. worker: queue -> API -> output/ + Cosmos DB  (idempotent)
#    host.json: maxDequeueCount = 5 -> inference-requests-poison
# ---------------------------------------------------------------------------
@app.function_name("worker")
@app.queue_trigger(arg_name="qmsg", queue_name=QUEUE, connection="AzureWebJobsStorage")
def worker(qmsg: func.QueueMessage) -> None:
    job = json.loads(qmsg.get_body().decode())
    name, etag = job["blobName"], job["eTag"]
    doc_id = idempotency_key(name, etag)
    attempt = qmsg.dequeue_count

    from azure.cosmos.exceptions import CosmosResourceNotFoundError

    try:
        cosmos().read_item(doc_id, partition_key=name)
        log.info("already_processed", extra={"file": name, "id": doc_id})
        files_counter.add(1, {"outcome": "duplicate"})
        return
    except CosmosResourceNotFoundError:
        pass

    content = blob_service().get_blob_client(INPUT, name).download_blob().readall()
    records = parse_records(content)

    t0 = time.perf_counter()
    resp = http().post("/predict", json={"records": records, "source": name})
    call_ms = round((time.perf_counter() - t0) * 1000, 1)
    api_latency.record(call_ms)
    if resp.status_code != 200:
        files_counter.add(1, {"outcome": "failed"})
        log.error(
            "api_error",
            extra={"file": name, "status": resp.status_code, "attempt": attempt,
                   "body": resp.text[:300]},
        )
        # Raising makes the runtime retry; after 5 attempts -> -poison queue.
        raise RuntimeError(f"API returned {resp.status_code} for {name} (attempt {attempt})")

    result = resp.json()
    preds = result["predictions"]
    now = datetime.now(UTC)
    positives = sum(p["label"] for p in preds)
    summary = {
        "id": doc_id,
        "fileName": name,
        "eTag": etag,
        "timestamp": now.isoformat(),
        "day": now.strftime("%Y-%m-%d"),
        "rows": len(preds),
        "prediction": "malignant" if positives else "benign",
        "positives": positives,
        "confidence": round(sum(max(p["probability"], 1 - p["probability"]) for p in preds)
                            / len(preds), 4),
        "modelVersion": result["model_version"],
        "apiVersion": result["api_version"],
        "modelLatencyMs": result["latency_ms"],
        "apiCallMs": call_ms,
        "attempt": attempt,
    }
    if job.get("eventTime"):
        upload = datetime.fromisoformat(job["eventTime"])
        summary["endToEndMs"] = round((now - upload).total_seconds() * 1000, 1)
        e2e_latency.record(summary["endToEndMs"])

    blob_service().get_blob_client(OUTPUT, f"{name}.json").upload_blob(
        json.dumps({**summary, "predictions": preds}, indent=2), overwrite=True
    )
    cosmos().upsert_item(summary)
    files_counter.add(1, {"outcome": "processed"})
    log.info("file_processed", extra=summary)


# ---------------------------------------------------------------------------
# 3. recent: GET /api/recent -> 20 latest inferences (CORS set on the app)
# ---------------------------------------------------------------------------
@app.function_name("recent")
@app.route(route="recent", methods=["GET"])
def recent(req: func.HttpRequest) -> func.HttpResponse:
    limit = min(int(req.params.get("limit", "20")), 100)
    items = list(
        cosmos().query_items(
            f"SELECT TOP {limit} c.id, c.fileName, c.timestamp, c.rows, c.prediction, "
            "c.positives, c.confidence, c.modelVersion, c.modelLatencyMs, c.apiCallMs, "
            "c.endToEndMs FROM c ORDER BY c.timestamp DESC",
            enable_cross_partition_query=True,
        )
    )
    return func.HttpResponse(
        json.dumps(items), mimetype="application/json", headers={"Cache-Control": "no-store"}
    )
