"""PyTorch MLP and artifact loading helpers for N6."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import torch
from torch import nn

# systemd deliberately enables MemoryDenyWriteExecute.  Disable oneDNN's runtime
# primitive/JIT path so CPU inference stays compatible with that hardening policy.
torch.backends.mkldnn.enabled = False
torch.set_num_threads(1)


class RaceMLP(nn.Module):
    """Compact CPU-friendly neural network that emits one logit per runner."""

    def __init__(self, input_dim: int, hidden_dims: tuple[int, ...] = (128, 64, 32), dropout: float = 0.15):
        super().__init__()
        layers: list[nn.Module] = []
        previous = input_dim
        for width in hidden_dims:
            layers.extend([
                nn.Linear(previous, width),
                nn.LayerNorm(width),
                nn.GELU(),
                nn.Dropout(dropout),
            ])
            previous = width
        layers.append(nn.Linear(previous, 1))
        self.network = nn.Sequential(*layers)

    def forward(self, values: torch.Tensor) -> torch.Tensor:
        return self.network(values).squeeze(-1)


@dataclass
class LoadedN6Model:
    model: RaceMLP
    preprocessor: Any
    metadata: dict[str, Any]


def load_model_bundle(model_path: Path, preprocessor_path: Path) -> LoadedN6Model:
    """Load only N6-owned artifacts, never an artifact from the V10 project."""
    if not model_path.is_file() or not preprocessor_path.is_file():
        raise FileNotFoundError("N6 model artifacts are not present; run training before inference.")
    payload = torch.load(model_path, map_location="cpu", weights_only=False)
    if payload.get("artifact_type") != "n6_race_mlp":
        raise ValueError("Unexpected N6 model artifact type.")
    model = RaceMLP(
        input_dim=int(payload["input_dim"]),
        hidden_dims=tuple(int(value) for value in payload["hidden_dims"]),
        dropout=float(payload["dropout"]),
    )
    model.load_state_dict(payload["state_dict"])
    model.eval()
    return LoadedN6Model(model=model, preprocessor=joblib.load(preprocessor_path), metadata=payload)
