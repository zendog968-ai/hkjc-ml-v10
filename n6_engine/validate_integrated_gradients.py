#!/usr/bin/env python3
"""Check Integrated Gradients completeness convergence on the N6 production MLP."""

from __future__ import annotations

import json

import numpy as np
import torch

from analyze_model import chronological_test_split
from n6.config import MODEL_PATH, PREPROCESSOR_PATH
from n6.feature_engineering import load_training_frame
from n6.model import load_model_bundle


def attributes(model: torch.nn.Module, inputs: np.ndarray, baseline: np.ndarray, steps: int) -> tuple[float, float, float]:
    x = torch.tensor(inputs, dtype=torch.float32)
    b = torch.tensor(baseline, dtype=torch.float32)
    delta = x - b
    weights = torch.ones(steps + 1, dtype=torch.float32)
    weights[0] = weights[-1] = 0.5
    total = torch.zeros_like(x)
    for alpha, weight in zip(torch.linspace(0.0, 1.0, steps + 1), weights):
        point = (b + alpha * delta).detach().requires_grad_(True)
        gradient = torch.autograd.grad(model(point).sum(), point)[0]
        total += weight * gradient
    attribution = delta * total / steps
    with torch.no_grad():
        output_delta = model(x) - model(b.expand_as(x))
    error = torch.abs(output_delta - attribution.sum(dim=1))
    return float(error.mean()), float(torch.abs(output_delta).mean()), float(error.max())


def main() -> int:
    bundle = load_model_bundle(MODEL_PATH, PREPROCESSOR_PATH)
    train = load_training_frame()
    test = chronological_test_split(train)
    contract = bundle.metadata["feature_contract"]
    baseline = np.asarray(bundle.preprocessor.transform(train.loc[train.race_date <= train.race_date.quantile(0.70), contract]), dtype=np.float32).mean(axis=0)
    values = np.asarray(bundle.preprocessor.transform(test.loc[:, contract].iloc[:64]), dtype=np.float32)
    result = {str(step): dict(zip(("mean_abs_error", "mean_abs_output_delta", "max_abs_error"), attributes(bundle.model, values, baseline, step))) for step in (32, 64, 128, 256, 512)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
