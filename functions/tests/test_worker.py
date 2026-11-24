"""Worker behaviour with in-memory fakes for Blob Storage, Cosmos DB and the API."""

import json
from pathlib import Path

import httpx
import pytest
from azure.cosmos.exceptions import CosmosResourceNotFoundError

import function_app as fa

SAMPLES = Path(__file__).resolve().parents[2] / "samples"


class FakeBlob:
    def __init__(self, store, key):
        self.store, self.key = store, key

    def download_blob(self):
        data = self.store[self.key]
        return type("D", (), {"readall": lambda _self: data})()

    def upload_blob(self, data, overwrite=False, **_):
        self.store[self.key] = data.encode() if isinstance(data, str) else data


class FakeBlobService:
    def __init__(self):
        self.store = {}

    def get_blob_client(self, container, name):
        return FakeBlob(self.store, (container, name))


class FakeCosmos:
    def __init__(self):
        self.items = {}
        self.upserts = 0

    def read_item(self, item, partition_key):
        if (item, partition_key) not in self.items:
            raise CosmosResourceNotFoundError(message="nf", response=None)
        return self.items[(item, partition_key)]

    def upsert_item(self, doc):
        self.upserts += 1
        self.items[(doc["id"], doc["fileName"])] = doc


def fake_api(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    try:
        probs = [min(float(r["mean_radius"]) / 30, 1.0) for r in body["records"]]
    except ValueError:
        return httpx.Response(422, json={"detail": "not a number"})
    return httpx.Response(200, json={
        "model_version": "1.0.0", "api_version": "1.0.0", "latency_ms": 0.5,
        "predictions": [{"label": int(p >= .5), "class_name": "x", "probability": p} for p in probs],
    })


@pytest.fixture
def env(monkeypatch):
    blobs, cosmos = FakeBlobService(), FakeCosmos()
    monkeypatch.setattr(fa, "blob_service", lambda: blobs)
    monkeypatch.setattr(fa, "cosmos", lambda: cosmos)
    client = httpx.Client(base_url="http://api", transport=httpx.MockTransport(fake_api))
    monkeypatch.setattr(fa, "http", lambda: client)
    return blobs, cosmos


def make_msg(name, etag="0x1", dequeue=1):
    body = json.dumps({"blobName": name, "eTag": etag, "size": 1, "rows": 1,
                       "eventTime": "2026-11-24T10:00:00+00:00"})
    return type("Msg", (), {"get_body": lambda _s: body.encode(), "dequeue_count": dequeue})()


def test_valid_file_written_once(env):
    blobs, cosmos = env
    blobs.store[("input", "a.csv")] = (SAMPLES / "valid_batch.csv").read_bytes()
    fa.worker(make_msg("a.csv"))
    assert ("output", "a.csv.json") in blobs.store
    assert cosmos.upserts == 1
    doc = next(iter(cosmos.items.values()))
    assert doc["rows"] == 10 and doc["modelVersion"] == "1.0.0"

    # Same message delivered again (at-least-once): no new write.
    fa.worker(make_msg("a.csv", dequeue=2))
    assert cosmos.upserts == 1


def test_new_etag_is_reprocessed(env):
    blobs, cosmos = env
    blobs.store[("input", "a.csv")] = (SAMPLES / "valid_batch.csv").read_bytes()
    fa.worker(make_msg("a.csv", etag="0x1"))
    fa.worker(make_msg("a.csv", etag="0x2"))
    assert cosmos.upserts == 2


def test_bad_values_raise_for_retry(env):
    blobs, cosmos = env
    blobs.store[("input", "p.csv")] = (SAMPLES / "poison_values.csv").read_bytes()
    with pytest.raises(RuntimeError, match="422"):
        fa.worker(make_msg("p.csv", dequeue=3))
    assert cosmos.upserts == 0 and ("output", "p.csv.json") not in blobs.store
