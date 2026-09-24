from __future__ import annotations

import asyncio
import time

from .models import Action, Controls, SafetySettings
from .runtime import RuntimeAdapter, RuntimeFault


class Rejected(RuntimeError):
    pass


def requires_approval(action: Action, safety: SafetySettings, state: dict) -> bool:
    a = action.template
    field = state.get("fields", {}).get(a.target, {})
    if field.get("sensitive"):
        # Explicit human entry only: never bind secrets to automatic form facts in this version.
        raise Rejected("SENSITIVE_FIELD_REQUIRES_MANUAL_INPUT")
    if a.kind == "control":
        if not safety.allow_realtime:
            raise Rejected("REALTIME_PERMISSION_REQUIRED")
        return False
    if a.kind in {"click", "tap", "key", "select"}:
        return not safety.allow_auto_click
    return False


class MotorLease:
    """Bounded control lease. Called under the session's sole execution gate.

    The watchdog acquires the SAME gate: expiry can never race a newer key-down.
    This is soft real time, not a hard real-time or OS-crash-proof guarantee.
    """

    def __init__(self, runtime: RuntimeAdapter, gate: asyncio.Lock):
        self.runtime, self.gate = runtime, gate
        self._generation = 0
        self._last_sequence = -1
        self._timer: asyncio.Task | None = None
        self.failure: str | None = None

    async def apply(self, action: Action) -> None:
        if not self.runtime.capabilities.realtime_control:
            raise Rejected("RUNTIME_HAS_NO_PERSISTENT_CONTROL")
        if action.sequence <= self._last_sequence:
            raise Rejected("OUT_OF_ORDER_CONTROL_FRAME")
        expires = min(action.deadline, time.monotonic() + action.template.ttl_ms / 1000)
        if expires <= time.monotonic():
            raise Rejected("CONTROL_FRAME_EXPIRED")
        self._last_sequence = action.sequence
        self._generation += 1
        generation = self._generation
        if self._timer:
            self._timer.cancel()
        try:
            await self.runtime.set_controls(action.template.controls)
        except BaseException:
            await self.runtime.set_controls(Controls())
            raise
        self._timer = asyncio.create_task(self._expire(generation, expires))

    async def _expire(self, generation: int, expires: float) -> None:
        try:
            await asyncio.sleep(max(0, expires - time.monotonic()))
            async with self.gate:
                if generation == self._generation:
                    await self.runtime.set_controls(Controls())
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self.failure = type(exc).__name__

    async def neutral(self) -> None:
        self._generation += 1
        if self._timer:
            self._timer.cancel()
            self._timer = None
        await self.runtime.set_controls(Controls())
