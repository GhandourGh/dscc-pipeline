"""Train the pipeline's classifier and export it with a semantic version.

Dataset: Breast Cancer Wisconsin (Diagnostic), UCI Machine Learning Repository,
https://archive.ics.uci.edu/dataset/17 - licence CC BY 4.0. It ships inside
scikit-learn, so training needs no download and is fully reproducible.

Task: binary classification of a tumour as malignant (1) or benign (0) from 30
numeric features computed on a digitised image of a fine-needle aspirate.

Usage:  python model/train.py [--version 1.0.0]
Writes: model/model-<version>.joblib  (sklearn Pipeline)
        model/model-<version>.json    (feature list, metrics, metadata)
        samples/*.csv                 (demo files for the pipeline)
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.datasets import load_breast_cancer
from sklearn.dummy import DummyClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

SEED = 42
ROOT = Path(__file__).resolve().parent.parent


def load_data() -> tuple[pd.DataFrame, pd.Series]:
    raw = load_breast_cancer(as_frame=True)
    X = raw.data.copy()
    X.columns = [c.replace(" ", "_") for c in X.columns]
    # sklearn encodes malignant=0, benign=1; flip so the positive class is malignant.
    y = (1 - raw.target).rename("malignant")
    return X, y


def evaluate(model, X_test, y_test) -> dict:
    proba = model.predict_proba(X_test)[:, 1]
    pred = (proba >= 0.5).astype(int)
    return {
        "auc": round(float(roc_auc_score(y_test, proba)), 4),
        "f1": round(float(f1_score(y_test, pred, zero_division=0)), 4),
        "accuracy": round(float(accuracy_score(y_test, pred)), 4),
    }


def write_samples(X_test: pd.DataFrame) -> None:
    out = ROOT / "samples"
    out.mkdir(exist_ok=True)
    X_test.head(10).to_csv(out / "valid_batch.csv", index=False)
    # Missing a required column -> rejected by the dispatcher.
    X_test.head(5).drop(columns=["mean_radius"]).to_csv(out / "invalid_schema.csv", index=False)
    (out / "invalid_extension.txt").write_text("not a csv file\n")
    # Correct header, but a non-numeric value -> passes the dispatcher, the API
    # answers 422, the worker fails 5 times and the message lands in -poison.
    poison = X_test.head(3).copy().astype(object)
    poison.iloc[1, 0] = "not-a-number"
    poison.to_csv(out / "poison_values.csv", index=False)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="1.0.0")
    args = parser.parse_args()
    np.random.seed(SEED)

    X, y = load_data()
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, stratify=y, random_state=SEED
    )

    # Fit on plain numpy arrays so the API can serve without pandas (smaller image).
    # Column order is fixed by meta["features"].
    Xtr, Xte = X_train.to_numpy(dtype=float), X_test.to_numpy(dtype=float)
    baseline = DummyClassifier(strategy="prior").fit(Xtr, y_train)
    model = Pipeline(
        [
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(C=1.0, max_iter=2000, random_state=SEED)),
        ]
    ).fit(Xtr, y_train)

    metrics = {"model": evaluate(model, Xte, y_test), "baseline": evaluate(baseline, Xte, y_test)}

    # Inference latency on CPU for a single row (requirement: < 1 s).
    t0 = time.perf_counter()
    for _ in range(100):
        model.predict_proba(Xte[:1])
    single_row_ms = (time.perf_counter() - t0) * 10

    model_path = ROOT / "model" / f"model-{args.version}.joblib"
    joblib.dump(model, model_path)
    meta = {
        "version": args.version,
        "task": "binary classification (1 = malignant, 0 = benign)",
        "dataset": "Breast Cancer Wisconsin (Diagnostic), UCI ML Repository, CC BY 4.0",
        "features": list(X.columns),
        "classes": {"0": "benign", "1": "malignant"},
        "train_rows": int(len(X_train)),
        "test_rows": int(len(X_test)),
        "seed": SEED,
        "metrics_test": metrics,
        "single_row_latency_ms": round(single_row_ms, 3),
        "model_size_kb": round(model_path.stat().st_size / 1024, 1),
        "sklearn_version": sklearn.__version__,
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (ROOT / "model" / f"model-{args.version}.json").write_text(json.dumps(meta, indent=2))
    write_samples(X_test)
    print(json.dumps(meta["metrics_test"], indent=2))
    print(f"single-row latency: {single_row_ms:.2f} ms, size: {meta['model_size_kb']} KB")


if __name__ == "__main__":
    main()
