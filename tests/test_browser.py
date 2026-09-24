from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from laya_runtime.models import Action, Mode, RunState, SafetySettings
from laya_runtime.policies import DemoPolicy
from laya_runtime.profiles import builtin_profiles
from laya_runtime.runtime import BrowserRuntime
from laya_runtime.service import AgentService
from laya_runtime.storage import EventStore


@pytest.fixture
async def browser():
    executable = os.getenv("BROWSER_EXECUTABLE") or shutil.which("chromium")
    try:
        html = (Path(__file__).parents[1] / "src/laya_runtime/static/form.html").read_text()
        runtime = await BrowserRuntime.create(None, executable_path=executable, inline_html=html)
    except Exception as exc:
        pytest.fail(f"Chromium integration could not start: {exc}; install it with agentctl browser-install")
    yield runtime
    await runtime.close()


async def wait_state(s, states, timeout=10):
    deadline = time.monotonic()+timeout
    while s.state not in states and time.monotonic()<deadline:
        await asyncio.sleep(.025)
    assert s.state in states, f"Unexpected state: {s.public()}"


@pytest.mark.browser
async def test_real_chromium_form_closed_loop_with_explicit_demo_policy(browser, tmp_path):
    service = AgentService(DemoPolicy(), EventStore(tmp_path / "events.db"))
    s = await service.add(browser, builtin_profiles()["form-demo"], "Fill supplied form",
        {"name": "Alice-PRIVATE", "company": "Company-PRIVATE"}, Mode.AUTO,
        SafetySettings(allow_auto_click=True))
    try:
        await service.resume(s)
        await wait_state(s, {RunState.COMPLETED, RunState.NEEDS_CONTEXT})
        assert s.state == RunState.COMPLETED
        assert s.steps == 3
        assert await browser.page.locator("#name").input_value() == "Alice-PRIVATE"
        assert await browser.page.locator("#result").inner_text() == "Saved"
        events = await service.store.events(s.id)
        assert len([e for e in events if e["type"]=="AGENT_DECISION"]) == 3
        assert all(e["payload"]["provider"] == "demo-NOT-LAYA" for e in events if e["type"]=="AGENT_DECISION")
        assert "Alice-PRIVATE" not in json.dumps(events)
        assert "Company-PRIVATE" not in json.dumps(events)
    finally:
        await service.close()


@pytest.mark.browser
async def test_assist_requires_single_use_approval(browser, tmp_path):
    service = AgentService(DemoPolicy(), EventStore(tmp_path / "events.db"))
    s = await service.add(browser, builtin_profiles()["form-demo"], "Fill form", {"name": "A", "company": "B"})
    try:
        await service.resume(s)
        await wait_state(s, {RunState.AWAITING_APPROVAL})
        assert await browser.page.locator("#name").input_value() == ""
        action_id = s.pending.id
        result = await service.approve(s, action_id)
        assert result.status == "executed"
        assert await browser.page.locator("#name").input_value() == "A"
        with pytest.raises(ValueError):
            await service.approve(s, action_id)
    finally:
        await service.close()


@pytest.mark.browser
async def test_replaced_dom_node_invalidates_approval(browser, tmp_path):
    service = AgentService(DemoPolicy(), EventStore(tmp_path / "events.db"))
    s = await service.add(browser, builtin_profiles()["form-demo"], "Fill name", {"name": "A"})
    try:
        await service.resume(s)
        await wait_state(s, {RunState.AWAITING_APPROVAL})
        action = s.pending
        await browser.page.locator("#name").evaluate("el=>el.replaceWith(el.cloneNode(true))")
        result = await service.approve(s, action.id)
        assert result.status == "rejected"
        assert result.code == "STALE_STATE_OR_TARGET"
        assert await browser.page.locator("#name").input_value() == ""
    finally:
        await service.close()


@pytest.mark.browser
async def test_changed_nonempty_value_invalidates_fingerprint_without_leaking_value(browser):
    pack = builtin_profiles()["form-demo"]
    await browser.page.locator("#name").fill("PRIVATE-ONE")
    first = await browser.observe(pack)
    await browser.page.locator("#name").fill("PRIVATE-TWO")
    second = await browser.observe(pack)
    assert first.fingerprint != second.fingerprint
    assert "PRIVATE" not in second.model_dump_json()
    assert second.state["fields"]["name"]["empty"] is False


@pytest.mark.browser
async def test_screenshot_does_not_change_semantic_observation(browser):
    pack = builtin_profiles()["form-demo"]
    first = await browser.observe(pack)
    png = await browser.screenshot()
    second = await browser.observe(pack)
    assert png.startswith(b"\x89PNG")
    assert first.fingerprint == second.fingerprint


@pytest.mark.browser
async def test_control_lease_releases_real_browser_keys(browser):
    from laya_runtime.control import MotorLease
    from laya_runtime.models import ActionTemplate, Controls, Versions
    html = (Path(__file__).parents[1] / "src/laya_runtime/static/reaction.html").read_text()
    await browser.page.set_content(html)
    await browser.page.evaluate("window.keyTrace=[];addEventListener('keydown',e=>keyTrace.push(['down',e.key]));addEventListener('keyup',e=>keyTrace.push(['up',e.key]));")
    gate = asyncio.Lock()
    motor = MotorLease(browser, gate)
    action = Action(session_id="s", source="laya", template=ActionTemplate(id="right", description="right",
        kind="control", controls=Controls(keys=("ArrowRight",)), ttl_ms=80),
        versions=Versions(goal=1, control=1, policy=1, episode="e"),
        observation_id="o", fingerprint="f", deadline=time.monotonic()+2, sequence=1)
    async with gate:
        await motor.apply(action)
    assert browser._held == {"ArrowRight"}
    await asyncio.sleep(.2)
    assert await browser.page.evaluate("keyTrace") == [["down", "ArrowRight"], ["up", "ArrowRight"]]
    assert await browser.page.locator("#keys").inner_text() == "none"
    assert not browser._held


@pytest.mark.browser
async def test_human_click_takes_ownership_and_releases_controls(browser, tmp_path):
    from laya_runtime.models import Controls
    service = AgentService(DemoPolicy(), EventStore(tmp_path / "events.db"))
    s = await service.add(browser, builtin_profiles()["form-demo"], "Fill form", {"name": "A"})
    try:
        await browser.set_controls(Controls(keys=("w",)))
        epoch = s.control_epoch
        await service.manual(s, "click", {"x": .1, "y": .1})
        assert s.owner == "human" and s.state == RunState.PAUSED
        assert s.control_epoch > epoch
        assert not browser._held
    finally:
        await service.close()


@pytest.mark.browser
@pytest.mark.skipif(os.getenv("RUN_NETWORK_BROWSER") != "1", reason="Container browser network navigation is administratively blocked; guard isolation requires a network-enabled browser")
async def test_outbound_navigation_to_unapproved_origin_is_blocked(browser, fixture_server):
    # Establish a reachable server first: a closed port would be a false-positive guard test.
    baseline = await browser._browser.new_context()
    try:
        page = await baseline.new_page()
        response = await page.goto(fixture_server + "/form.html", timeout=3000)
        assert response.status == 200
        with pytest.raises(Exception):
            await browser.page.goto(fixture_server + "/form.html", timeout=1500)
    finally:
        await baseline.close()


@pytest.mark.browser
async def test_approval_renews_freshness_only_after_rechecking_identical_state(browser, tmp_path):
    service = AgentService(DemoPolicy(), EventStore(tmp_path / "events.db"))
    s = await service.add(browser, builtin_profiles()["form-demo"], "Fill form", {"name": "A"})
    try:
        await service.resume(s)
        await wait_state(s, {RunState.AWAITING_APPROVAL})
        s.pending = s.pending.model_copy(update={"deadline": time.monotonic()-1})
        result = await service.approve(s, s.pending.id)
        assert result.status == "executed"
        assert await browser.page.locator("#name").input_value() == "A"
    finally:
        await service.close()


@pytest.mark.browser
async def test_reactive_sandbox_runs_control_frames_and_stops_on_takeover(browser, tmp_path):
    html = (Path(__file__).parents[1] / "src/laya_runtime/static/reaction.html").read_text()
    await browser.page.set_content(html)
    service = AgentService(DemoPolicy(), EventStore(tmp_path / "events.db"))
    s = await service.add(browser, builtin_profiles()["reaction-sandbox"], "Track the target", {},
                          Mode.AUTO, SafetySettings(allow_realtime=True))
    try:
        await service.resume(s)
        for _ in range(100):
            if s.steps >= 3 or s.state != RunState.RUNNING:
                break
            await asyncio.sleep(.02)
        assert s.steps >= 3, s.public()
        await service.interrupt(s)
        count = s.steps
        await asyncio.sleep(.2)
        assert s.steps == count and not browser._held and s.owner == "human"
    finally:
        await service.close()


@pytest.mark.browser
@pytest.mark.live_laya
@pytest.mark.skipif(os.getenv("RUN_LIVE_LAYA") != "1", reason="Real model-to-browser release gate requires SDK and weights")
async def test_live_laya_drives_real_browser_form(browser, tmp_path):
    from laya_runtime.api import Settings, make_policy
    policy = make_policy(Settings())
    assert policy.name in {"laya-local", "laya-http"}, "Demo must never satisfy the real-model release gate"
    service = AgentService(policy, EventStore(tmp_path / "events.db"))
    try:
        await policy.warmup()
        s = await service.add(browser, builtin_profiles()["form-demo"], "Fill both required fields and submit",
            {"name": "Test User", "company": "Local Test"}, Mode.AUTO, SafetySettings(allow_auto_click=True))
        await service.resume(s)
        await wait_state(s, {RunState.COMPLETED, RunState.NEEDS_CONTEXT}, timeout=60)
        assert s.state == RunState.COMPLETED and s.steps == 3, s.public()
        assert await browser.page.locator("#result").inner_text() == "Saved"
    finally:
        await service.close()
