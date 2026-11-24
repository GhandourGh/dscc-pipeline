"""Pure validation helpers used by the dispatcher (unit-tested without Azure)."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = json.loads((Path(__file__).parent / "model_schema.json").read_text())
FEATURES: list[str] = _SCHEMA["features"]
ALLOWED_EXTENSIONS = (".csv",)


@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""
    rows: int = 0


def blob_name_from_subject(subject: str) -> str:
    """'/blobServices/default/containers/input/blobs/a/b.csv' -> 'a/b.csv'."""
    marker = "/blobs/"
    idx = subject.find(marker)
    if idx < 0:
        raise ValueError(f"unexpected Event Grid subject: {subject}")
    return subject[idx + len(marker):]


def check_metadata(name: str, size: int, max_bytes: int) -> ValidationResult:
    if not name.lower().endswith(ALLOWED_EXTENSIONS):
        return ValidationResult(False, f"extension not allowed (expected {ALLOWED_EXTENSIONS})")
    if size <= 0:
        return ValidationResult(False, "empty file")
    if size > max_bytes:
        return ValidationResult(False, f"file too large: {size} B > {max_bytes} B")
    return ValidationResult(True)


def check_schema(content: bytes, max_rows: int) -> ValidationResult:
    """Structural check only: header must contain every model feature.

    Cell values are deliberately NOT type-checked here: the API does that, and a
    bad value therefore exercises the retry -> poison path of the worker.
    """
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ValidationResult(False, "file is not UTF-8 text")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header:
        return ValidationResult(False, "missing CSV header")
    missing = [f for f in FEATURES if f not in {h.strip() for h in header}]
    if missing:
        return ValidationResult(False, f"missing columns: {', '.join(missing[:5])}"
                                + (" ..." if len(missing) > 5 else ""))
    rows = sum(1 for row in reader if any(cell.strip() for cell in row))
    if rows == 0:
        return ValidationResult(False, "no data rows")
    if rows > max_rows:
        return ValidationResult(False, f"too many rows: {rows} > {max_rows}")
    return ValidationResult(True, rows=rows)


def parse_records(content: bytes) -> list[dict[str, str]]:
    """CSV -> list of {feature: raw string}; the API coerces and validates types."""
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    return [
        {f: row[f].strip() for f in FEATURES}
        for row in reader
        if any((v or "").strip() for v in row.values())
    ]


def idempotency_key(blob_name: str, etag: str) -> str:
    return hashlib.sha256(f"{blob_name}|{etag.strip('\"')}".encode()).hexdigest()[:32]
