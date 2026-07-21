from __future__ import annotations

import os

import pytest
import torch


@pytest.mark.gpu
def test_production_gpu_executes_torch_workload() -> None:
    if os.getenv("DOCLING_SERVE_RUN_MODEL_TESTS") != "1":
        pytest.skip("DOCLING_SERVE_RUN_MODEL_TESTS is not configured")
    assert torch.cuda.is_available(), "production GPU runner has no CUDA device"
    left = torch.tensor([[1.0, 2.0]], device="cuda")
    right = torch.tensor([[3.0], [4.0]], device="cuda")
    result = left @ right
    assert result.item() == 11.0
    assert torch.cuda.get_device_properties(0).total_memory > 0
