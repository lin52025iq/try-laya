from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from laya_runtime.api import Settings, create_app
from laya_runtime.models import Capabilities, Controls, Observation
from laya_runtime.policies import DemoPolicy
from laya_runtime.profiles import builtin_profiles
from laya_runtime.runtime import AndroidRuntime, BrowserRuntime, RuntimeFault, origin


class APIRuntime:
    id = "api-test-runtime"
    capabilities = Capabilities(realtime_control=True)
    async def observe(self, profile):
        return Observation(runtime_id=self.id, state={"fields": {}}, fingerprint="fixture")
    async def set_controls(self, controls):
        pass
    async def manual(self, kind, payload):
        pass
    async def close(self):
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def create(*args, **kwargs):
        return APIRuntime()
    monkeypatch.setattr(BrowserRuntime, "create", create)
    app = create_app(Settings(data_dir=tmp_path, token="test-secret", testing=True), DemoPolicy())
    with TestClient(app) as client:
        yield client


AUTH = {"Authorization": "Bearer test-secret"}


def test_default_configuration_is_laya_not_demo(tmp_path):
    settings = Settings(data_dir=tmp_path)
    assert settings.policy_backend == "local"


def test_api_requires_token_and_marks_demo(client):
    assert client.get("/api/v1/health").status_code == 401
    assert client.get("/api/v1/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = client.get("/api/v1/health", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["simulation"] is True
    assert "NOT-LAYA" in response.json()["provider"]


def test_api_blocks_cross_origin_and_dns_rebinding_hosts(client):
    assert client.get("/api/v1/profiles", headers={**AUTH, "Origin": "https://evil.example"}).status_code == 403
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_profile_import_does_not_activate_a_session(client):
    pack = builtin_profiles()["form-demo"].model_dump(mode="json")
    result = client.post("/api/v1/profiles", json=pack, headers=AUTH)
    assert result.status_code == 200
    assert result.json()["activated"] is False
    assert client.get("/api/v1/sessions", headers=AUTH).json() == []


def test_profile_permissions_and_code_cannot_be_injected(client):
    pack = builtin_profiles()["form-demo"].model_dump(mode="json")
    pack["safety"] = {"allow_auto_click": True}
    assert client.post("/api/v1/profiles", json=pack, headers=AUTH).status_code == 422
    pack.pop("safety")
    pack["id"] = "../../secrets"
    assert client.post("/api/v1/profiles", json=pack, headers=AUTH).status_code == 422


def test_sessions_redact_facts_and_separate_mode_from_state(client):
    response = client.post("/api/v1/sessions", headers=AUTH,
        json={"facts": {"name": "PRIVATE-FORM-VALUE"}, "mode": "auto"})
    assert response.status_code == 200
    s = response.json()
    assert "PRIVATE-FORM-VALUE" not in response.text
    assert s["mode"] == "auto" and s["state"] == "idle"
    paused = client.post(f"/api/v1/sessions/{s['id']}/pause", json={}, headers=AUTH).json()
    assert paused["mode"] == "auto" and paused["state"] == "paused"
    assert paused["owner"] == "human"
    old_episode = paused["episode"]
    new_episode = client.post(f"/api/v1/sessions/{s['id']}/episodes", json={}, headers=AUTH).json()
    assert new_episode["episode"] != old_episode


def test_websocket_auth_and_event_delivery(client):
    s = client.post("/api/v1/sessions", json={}, headers=AUTH).json()
    with client.websocket_connect(f"/api/v1/ws/{s['id']}") as ws:
        ws.send_json({"token": "test-secret"})
        assert ws.receive_json()["type"] == "STREAM_READY"
        client.post(f"/api/v1/sessions/{s['id']}/pause", json={}, headers=AUTH)
        assert ws.receive_json()["type"] == "CONTROL_TAKEOVER"
    with client.websocket_connect(f"/api/v1/ws/{s['id']}") as ws:
        ws.send_json({"token": "invalid"})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_llm_is_optional_and_never_silently_substituted(client, monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    result = client.post("/api/v1/strategist/compile", headers=AUTH,
        json={"profile_id": "form-demo", "goal": "Fill form", "guide": "Guide"})
    assert result.status_code == 503


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///etc/passwd", "data:text/html,evil", "https://u:p@example.com"])
def test_unsafe_browser_urls(url):
    with pytest.raises(ValueError):
        origin(url)


@pytest.mark.parametrize("serial", ["-s evil", "localhost;rm -rf /", "$(evil)", "a\nb", ""])
def test_adb_serial_validation(serial):
    with pytest.raises(ValueError):
        AndroidRuntime(serial)


@pytest.mark.parametrize("text", ["$(reboot)", "a;reboot", "x&reboot", "`reboot`", "name\nreboot", "中文", "%s", "'x'"])
async def test_adb_text_remote_shell_metacharacters_rejected(text):
    adb = AndroidRuntime("emulator-5554")
    with pytest.raises(RuntimeFault):
        await adb._input_text(text)


async def test_adb_safe_text_is_one_bounded_command():
    adb = AndroidRuntime("emulator-5554")
    seen = []
    async def run(*args, **kwargs):
        seen.append(args)
        return b""
    adb._run = run
    await adb._input_text("Alex Smith")
    assert seen == [("shell", "input", "text", "Alex%sSmith")]
    assert adb.capabilities.realtime_control is False
    with pytest.raises(RuntimeFault):
        await adb.set_controls(Controls(keys=("w",)))


def test_android_tree_parsing_and_entity_rejection():
    nodes = AndroidRuntime.parse_tree(b'<?xml version="1.0"?><hierarchy><node resource-id="app:id/start" text="Start"/></hierarchy>UI hierarchy dumped')
    assert nodes[0]["text"] == "Start"
    with pytest.raises(RuntimeFault):
        AndroidRuntime.parse_tree(b'<!DOCTYPE hierarchy><hierarchy></hierarchy>')


def test_incomplete_manual_action_is_a_validation_error():
    from pydantic import ValidationError
    from laya_runtime.api import HumanInput
    for payload in ({"kind": "click"}, {"kind": "text"}, {"kind": "key", "key": "Meta+R"}):
        with pytest.raises(ValidationError):
            HumanInput.model_validate(payload)


async def test_attached_tab_supports_neutral_without_realtime_controls():
    from laya_runtime.models import Capabilities, Controls
    from laya_runtime.runtime import BrowserRuntime, RuntimeFault
    runtime = BrowserRuntime()
    runtime.capabilities = Capabilities(realtime_control=False, attached=True)
    await runtime.set_controls(Controls())
    with pytest.raises(RuntimeFault):
        await runtime.set_controls(Controls(keys=("ArrowRight",)))
