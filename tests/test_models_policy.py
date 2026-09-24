from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
import pytest
from pydantic import ValidationError

from laya_runtime.models import Condition, Controls, Observation, Profile
from laya_runtime.policies import LocalLaya, PolicyError, RemoteLaya, decode
from laya_runtime.profiles import ContextError, build_request, builtin_profiles, candidates


QUESTIONS = {"next": {"type": "choice", "instructions": "Choose", "criteria": {"a": "First", "b": "Second"}}}


def answer(choice="a", probs=None, confidence=.01, tokens=40):
    return {"answers": {"next": {"type": "choice", "choice": choice,
        "probabilities": probs or {"a": .9, "b": .1}, "confidence": confidence,
        "answer_confidence": .9}}, "routing": {"model": "multilingual"}, "usage": {"input_tokens": tokens}}


@pytest.mark.parametrize("op", ["eq", "ne", "truthy", "falsy", "lt", "lte", "gt", "gte"])
def test_missing_information_never_satisfies_condition(op):
    assert not Condition(path="missing.value", op=op, value=False).matches({})


@pytest.mark.parametrize("op,value,expected", [("eq", 3, True), ("ne", 2, True), ("gt", 2, True),
                                               ("lt", 4, True), ("gte", 3, True), ("lte", 2, False)])
def test_conditions(op, value, expected):
    assert Condition(path="hp", op=op, value=value).matches({"hp": 3}) == expected


def test_bool_not_number():
    assert not Condition(path="x", op="gt", value=0).matches({"x": True})
    assert not Condition(path="x", value=1).matches({"x": True})


@pytest.mark.parametrize("keys", [["Meta"], ["w", "w"], ["Control+C"], ["rm -rf /"], ["F5"]])
def test_controls_are_bounded(keys):
    with pytest.raises(ValidationError):
        Controls(keys=keys)


def test_no_executable_profile_fields():
    data = builtin_profiles()["form-demo"].model_dump(mode="json")
    data["python"] = "import os"
    with pytest.raises(ValidationError):
        Profile.model_validate(data)


def test_duplicate_or_unbound_actions_rejected():
    data = builtin_profiles()["form-demo"].model_dump(mode="json")
    data["actions"].append(data["actions"][0])
    with pytest.raises(ValidationError):
        Profile.model_validate(data)
    data["actions"].pop()
    data["actions"][0]["target"] = "missing"
    with pytest.raises(ValidationError):
        Profile.model_validate(data)


def test_compiler_never_sends_form_facts():
    pack = builtin_profiles()["form-demo"]
    obs = Observation(runtime_id="fixture", state={"fields": {"name": {"present": True, "empty": True}}}, fingerprint="f")
    options = candidates(pack, obs, {"name": "DO-NOT-SEND"})
    state, questions = build_request(pack, "Fill name", obs, options)
    assert len(options) == 1
    assert "DO-NOT-SEND" not in json.dumps([state, questions])
    assert "untrusted_observation" in state
    assert "__escalate__" in questions["next"]["criteria"]
    assert candidates(pack, obs, {}) == []


def test_oversized_context_escalates_not_silent_truncation():
    pack = builtin_profiles()["form-demo"]
    obs = Observation(runtime_id="fixture", state={"body": "x" * 5000}, fingerprint="f")
    with pytest.raises(ContextError):
        build_request(pack, "goal", obs, list(pack.actions))


def test_decode_uses_probability_not_entropy_confidence():
    result = decode(answer(confidence=.001), QUESTIONS, "laya-http", 4)
    assert result.probability == .9
    assert result.margin == pytest.approx(.8)


@pytest.mark.parametrize("response", [answer(choice="unknown"), answer(probs={"a": 1, "c": 0}),
    answer(probs={"a": float("nan"), "b": .1}), answer(probs={"a": .2, "b": .2}),
    answer(probs={"a": .1, "b": .9}), answer(tokens=1024), {"answers": {}}])
def test_bad_laya_responses_fail_closed(response):
    with pytest.raises(PolicyError):
        decode(response, QUESTIONS, "laya-http", 1)


async def test_real_http_adapter_wire_contract_with_mock_server():
    seen = []
    def handler(request):
        seen.append(request)
        assert request.headers["authorization"] == "Bearer test-token"
        if request.method == "GET":
            return httpx.Response(200, json={"loaded": ["multilingual"]})
        assert request.url.path == "/v1/systemone"
        assert json.loads(request.content) == {"state": {"x": 1}, "questions": QUESTIONS, "model": "multilingual"}
        return httpx.Response(200, json=answer())
    policy = RemoteLaya("http://laya.invalid", api_key="test-token", transport=httpx.MockTransport(handler))
    try:
        await policy.warmup()
        result = await policy.predict({"x": 1}, QUESTIONS, 1)
        assert result.candidate_id == "a"
        assert len(seen) == 2
    finally:
        await policy.close()


async def test_remote_refuses_redirects():
    policy = RemoteLaya("http://local.invalid", transport=httpx.MockTransport(
        lambda request: httpx.Response(307, headers={"location": "http://other.invalid/steal"})))
    try:
        with pytest.raises(PolicyError):
            await policy.predict({}, QUESTIONS, 1)
    finally:
        await policy.close()


async def test_local_inference_timeout_does_not_create_unbounded_thread_queue():
    started, release = threading.Event(), threading.Event()
    class FakeRouter:
        calls = 0
        def predict(self, *args, **kwargs):
            self.calls += 1
            started.set()
            release.wait(2)
            return answer()
        def unload(self):
            pass
    router = FakeRouter()
    policy = LocalLaya()
    policy._router, policy.ready = router, True
    try:
        with pytest.raises(PolicyError, match="deadline"):
            await policy.predict({}, QUESTIONS, .02)
        assert started.is_set()
        with pytest.raises(PolicyError, match="busy"):
            await policy.predict({}, QUESTIONS, .02)
        assert router.calls == 1
        release.set()
        await asyncio.sleep(.05)
        result = await policy.predict({}, QUESTIONS, 1)
        assert result.candidate_id == "a"
        assert router.calls == 2
    finally:
        release.set()
        await policy.close()


async def test_no_implicit_demo_when_local_laya_not_warm():
    policy = LocalLaya()
    try:
        with pytest.raises(PolicyError, match="not warm"):
            await policy.predict({}, QUESTIONS, 1)
    finally:
        await policy.close()
