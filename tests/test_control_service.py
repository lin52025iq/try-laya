from __future__ import annotations

import asyncio
import time

import pytest

from laya_runtime.control import MotorLease, Rejected
from laya_runtime.models import (Action, ActionTemplate, Capabilities, Controls, Decision,
                                ExecutionResult, Mode, Observation, RunState, SafetySettings, Versions)
from laya_runtime.policies import DemoPolicy
from laya_runtime.profiles import builtin_profiles
from laya_runtime.service import AgentService
from laya_runtime.storage import EventStore


class FakeRuntime:
    id = "test-runtime"
    capabilities = Capabilities(realtime_control=True)
    def __init__(self):
        self.controls, self.executed = [], []
        self.fail = False
        self.fingerprint = "v1"
    async def set_controls(self, controls):
        self.controls.append((time.monotonic(), controls))
    async def observe(self, profile):
        return Observation(runtime_id=self.id, state={"fields": {
            "name": {"present": True, "empty": True, "uid": "node-1", "count": 1},
        }}, fingerprint=self.fingerprint)
    async def execute(self, action, facts):
        self.executed.append(action.id)
        if self.fail:
            raise TimeoutError("Acknowledgement lost after input was sent")
    async def manual(self, kind, payload):
        pass
    async def close(self):
        pass


def frame(seq=1, ttl=60):
    return Action(session_id="s", source="laya", template=ActionTemplate(id="right", description="right",
        kind="control", controls=Controls(keys=("ArrowRight",)), ttl_ms=ttl),
        versions=Versions(goal=1, control=1, policy=1, episode="e"), observation_id="o", fingerprint="f",
        deadline=time.monotonic()+1, sequence=seq)


async def test_control_lease_expires_without_new_decisions():
    runtime, gate = FakeRuntime(), asyncio.Lock()
    motor = MotorLease(runtime, gate)
    async with gate:
        await motor.apply(frame(ttl=40))
    assert runtime.controls[-1][1].keys == ("ArrowRight",)
    await asyncio.sleep(.1)
    assert runtime.controls[-1][1].keys == ()


async def test_old_expiry_cannot_release_newer_frame():
    runtime, gate = FakeRuntime(), asyncio.Lock()
    motor = MotorLease(runtime, gate)
    async with gate:
        await motor.apply(frame(seq=1, ttl=60))
    await asyncio.sleep(.03)
    async with gate:
        await motor.apply(frame(seq=2, ttl=150))
    await asyncio.sleep(.06)
    assert runtime.controls[-1][1].keys == ("ArrowRight",)
    await asyncio.sleep(.13)
    assert runtime.controls[-1][1].keys == ()


async def test_control_sequence_and_expiry_are_checked():
    runtime, gate = FakeRuntime(), asyncio.Lock()
    motor = MotorLease(runtime, gate)
    async with gate:
        await motor.apply(frame(seq=2))
        with pytest.raises(Rejected, match="OUT_OF_ORDER"):
            await motor.apply(frame(seq=1))
        with pytest.raises(Rejected, match="EXPIRED"):
            await motor.apply(frame(seq=3).model_copy(update={"deadline": time.monotonic()-1}))
        await motor.neutral()
    assert runtime.controls[-1][1].keys == ()


class SlowPolicy(DemoPolicy):
    name = "test-delayed-policy-NOT-LAYA"
    def __init__(self):
        self.entered, self.release = asyncio.Event(), asyncio.Event()
    async def predict(self, state, questions, budget_s):
        self.entered.set()
        await self.release.wait()
        return Decision(candidate_id="fill_name", provider=self.name, probability=.99, margin=.98, latency_ms=10)


async def setup_service(tmp_path, policy=None):
    runtime = FakeRuntime()
    service = AgentService(policy or DemoPolicy(), EventStore(tmp_path / "test.db"))
    s = await service.add(runtime, builtin_profiles()["form-demo"], "Fill name", {"name": "PrivateValue"},
                          Mode.AUTO, SafetySettings(allow_auto_click=True))
    s.owner, s.state = "agent", RunState.RUNNING
    return service, s, runtime


@pytest.mark.parametrize("change", ["human", "goal", "profile", "episode"])
async def test_late_decision_after_context_change_never_executes(tmp_path, change):
    policy = SlowPolicy()
    service, s, runtime = await setup_service(tmp_path, policy)
    try:
        step = asyncio.create_task(service.step(s))
        await policy.entered.wait()
        assert not s.gate.locked(), "LLM/Laya inference must not hold the execution gate"
        if change == "human":
            await service.interrupt(s)
        elif change == "goal":
            await service.update_goal(s, "New goal", {})
        elif change == "profile":
            await service.activate_profile(s, s.profile)
        else:
            await service.new_episode(s)
        policy.release.set()
        await step
        assert runtime.executed == []
        assert s.owner == "human"
    finally:
        policy.release.set()
        await service.close()


async def test_runtime_cannot_be_shared_by_two_controlling_sessions(tmp_path):
    service, s, runtime = await setup_service(tmp_path)
    try:
        with pytest.raises(ValueError, match="only one"):
            await service.add(runtime, s.profile, "other", {})
    finally:
        await service.close()


async def test_failed_acknowledgement_is_unknown_and_not_retried(tmp_path):
    service, s, runtime = await setup_service(tmp_path)
    runtime.fail = True
    try:
        result = await service.step(s)
        assert result.status == "unknown"
        assert s.state == RunState.NEEDS_CONTEXT
        await service.step(s)
        assert len(runtime.executed) == 1
    finally:
        await service.close()


async def test_journal_preserves_unknown_on_crash_window_and_is_idempotent(tmp_path):
    store = EventStore(tmp_path / "db.sqlite")
    assert await store.admit("action1", "session1") is None
    existing = await store.admit("action1", "session1")
    assert existing.status == "unknown"
    final = ExecutionResult(action_id="action1", status="executed", code="OK")
    await store.finish(final)
    assert (await store.admit("action1", "session1")).status == "executed"
    await store.close()
    reopened = EventStore(tmp_path / "db.sqlite")
    assert (await reopened.admit("action1", "session1")).status == "executed"
    await reopened.close()


async def test_event_sequence_is_durable_and_live_queue_is_bounded(tmp_path):
    store = EventStore(tmp_path / "db.sqlite")
    queue = store.subscribe("s")
    await asyncio.gather(*(store.emit("s", "TEST", {"n": n}) for n in range(140)))
    history = await store.events("s", 0, 500)
    assert [e["seq"] for e in history] == list(range(1, 141))
    assert queue.qsize() == 128
    assert queue.get_nowait()["seq"] == 13
    store.unsubscribe("s", queue)
    await store.close()


async def test_realtime_cannot_resume_without_explicit_permission(tmp_path):
    service = AgentService(DemoPolicy(), EventStore(tmp_path / "test.db"))
    s = await service.add(FakeRuntime(), builtin_profiles()["reaction-sandbox"], "Track target", {}, Mode.AUTO)
    try:
        with pytest.raises(ValueError, match="explicit realtime permission"):
            await service.resume(s)
        assert s.owner == "human" and s.state == RunState.IDLE
    finally:
        await service.close()


async def test_sensitive_field_blocks_and_pauses_instead_of_repeated_inference(tmp_path):
    service, s, runtime = await setup_service(tmp_path)
    original = runtime.observe
    async def sensitive(profile):
        obs = await original(profile)
        obs.state["fields"]["name"]["sensitive"] = True
        return obs
    runtime.observe = sensitive
    try:
        await service.step(s)
        assert s.state == RunState.NEEDS_CONTEXT and not runtime.executed
        attempts = s.attempts
        await service.step(s)
        assert s.attempts == attempts
    finally:
        await service.close()


async def test_closing_session_releases_runtime_ownership(tmp_path):
    service, s, runtime = await setup_service(tmp_path)
    try:
        await service.remove(s)
        assert s.id not in service.sessions
        replacement = await service.add(runtime, s.profile, "Another task", {})
        assert replacement.id != s.id
        assert (await service.store.events(s.id))[-1]["type"] == "SESSION_CLOSED"
    finally:
        await service.close()


async def test_partial_keydown_ack_failure_still_sends_keyup():
    from types import SimpleNamespace
    from laya_runtime.runtime import BrowserRuntime
    trace = []
    async def down(key):
        trace.append(("down", key))
        raise TimeoutError("Key reached browser, acknowledgement lost")
    async def up(key):
        trace.append(("up", key))
    runtime = BrowserRuntime()
    runtime.page = SimpleNamespace(keyboard=SimpleNamespace(down=down, up=up))
    gate = asyncio.Lock()
    motor = MotorLease(runtime, gate)
    async with gate:
        with pytest.raises(TimeoutError):
            await motor.apply(frame())
    assert trace == [("down", "ArrowRight"), ("up", "ArrowRight")]
    assert not runtime._held
