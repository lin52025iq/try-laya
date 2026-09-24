"""Explicit release gates. These tests must not be counted as passed when skipped."""
from __future__ import annotations

import os

import pytest

from laya_runtime.api import Settings
from laya_runtime.cli import benchmark
from laya_runtime.runtime import AndroidRuntime


@pytest.mark.live_laya
@pytest.mark.skipif(os.getenv("RUN_LIVE_LAYA") != "1", reason="Real Laya weights not requested; requires installed SDK/model")
async def test_live_laya_inference_smoke():
    result = await benchmark(Settings(), 3)
    assert result["simulation"] is False
    assert result["correct"] == 3, "Base model does not yet satisfy this fixture; do not declare deployment ready"


@pytest.mark.android
@pytest.mark.skipif(not os.getenv("TEST_ADB_SERIAL"), reason="No explicitly authorised ADB device supplied")
async def test_live_android_capture():
    device = AndroidRuntime(os.environ["TEST_ADB_SERIAL"])
    await device.health()
    png = await device.screenshot()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
