#!/usr/bin/env python3
"""Verify candidate-only Bayesian dependencies coexist with the N6 runtime."""

from __future__ import annotations

import json

import arviz
import numpy
import pymc
import torch

from n6.model import load_model_bundle
from n6.config import MODEL_PATH, PREPROCESSOR_PATH


def main() -> int:
    bundle = load_model_bundle(MODEL_PATH, PREPROCESSOR_PATH)
    report = {
        "numpy": numpy.__version__,
        "torch": torch.__version__,
        "pymc": pymc.__version__,
        "arviz": arviz.__version__,
        "active_model_input_dim": bundle.metadata.get("input_dim"),
        "active_model_release": bundle.metadata.get("production_release"),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
