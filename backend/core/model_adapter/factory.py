"""Adapter 工厂:按 MODEL_PROVIDER 选择实现。"""
from __future__ import annotations

from backend.config import settings
from backend.core.model_adapter.base import ModelAdapter
from backend.core.model_adapter.mock_adapter import MockAdapter


def build_adapter() -> ModelAdapter:
    if settings.model_provider == "ark":
        from backend.core.model_adapter.ark_adapter import ArkAdapter

        return ArkAdapter()
    return MockAdapter()
