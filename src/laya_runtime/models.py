from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Mode(StrEnum):
    MANUAL = "manual"
    ASSIST = "assist"
    AUTO = "auto"


class RunState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    NEEDS_CONTEXT = "needs_context"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"


class Condition(Schema):
    path: str = Field(min_length=1, max_length=150, pattern=r"^[A-Za-z0-9_.-]+$")
    op: Literal["eq", "ne", "lt", "lte", "gt", "gte", "truthy", "falsy"] = "eq"
    value: str | int | float | bool | None = None

    def matches(self, state: dict[str, Any]) -> bool:
        node: Any = state
        for part in self.path.split("."):
            if not isinstance(node, dict) or part not in node:
                return False  # Missing information must NEVER satisfy a negative condition.
            node = node[part]
        if self.op == "truthy":
            return bool(node)
        if self.op == "falsy":
            return not bool(node)
        if self.op == "eq":
            return type(node) is type(self.value) and node == self.value
        if self.op == "ne":
            return type(node) is type(self.value) and node != self.value
        if isinstance(node, bool) or isinstance(self.value, bool):
            return False
        if not isinstance(node, (float, int)) or not isinstance(self.value, (float, int)):
            return False
        if not math.isfinite(node) or not math.isfinite(self.value):
            return False
        return {"lt": node < self.value, "lte": node <= self.value,
                "gt": node > self.value, "gte": node >= self.value}[self.op]


class Probe(Schema):
    selector: str = Field(min_length=1, max_length=300)
    kind: Literal["element", "number", "text"] = "element"


SAFE_KEYS = {"ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "w", "a", "s", "d", "Space"}


class Controls(Schema):
    keys: tuple[str, ...] = ()
    dx: float = Field(default=0, ge=-100, le=100)
    dy: float = Field(default=0, ge=-100, le=100)

    @model_validator(mode="after")
    def validate_keys(self) -> Controls:
        if len(self.keys) != len(set(self.keys)) or not set(self.keys) <= SAFE_KEYS:
            raise ValueError("ControlFrame contains unsupported or duplicated keys")
        return self


class ActionTemplate(Schema):
    id: str = Field(min_length=1, max_length=48, pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$")
    description: str = Field(min_length=1, max_length=160)
    kind: Literal["click", "fill", "select", "key", "tap", "control"]
    target: str | None = None
    value_ref: str | None = Field(default=None, max_length=100)
    key: Literal["Tab", "Enter", "Escape", "Backspace", "ArrowDown", "ArrowUp"] | None = None
    controls: Controls | None = None
    ttl_ms: int = Field(default=100, ge=20, le=250)
    when: tuple[Condition, ...] = ()
    expect: tuple[Condition, ...] = ()

    @model_validator(mode="after")
    def shape(self) -> ActionTemplate:
        if self.kind in {"click", "fill", "select", "tap"} and not self.target:
            raise ValueError("UI actions must reference a probe, not an arbitrary command")
        if self.kind in {"fill", "select"} and not self.value_ref:
            raise ValueError("Text comes from a fact reference, never from Laya output")
        if self.kind == "key" and not self.key:
            raise ValueError("key action requires a bounded key name")
        if self.kind == "control" and self.controls is None:
            raise ValueError("control action requires controls")
        return self


class Profile(Schema):
    schema_version: Literal[1] = 1
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=100)
    domain: Literal["browser", "android", "realtime"] = "browser"
    knowledge_summary: str = Field(default="", max_length=1800)
    decision_instructions: str = Field(min_length=1, max_length=800)
    probes: dict[str, Probe] = Field(default_factory=dict, max_length=32)
    actions: tuple[ActionTemplate, ...] = Field(min_length=1, max_length=16)
    done_when: tuple[Condition, ...] = ()
    max_observation_age_ms: int = Field(default=3000, ge=40, le=10000)
    tick_ms: int = Field(default=100, ge=20, le=3000)
    max_steps: int = Field(default=100, ge=1, le=10000)
    provenance: str = Field(default="human", max_length=200)

    @model_validator(mode="after")
    def references(self) -> Profile:
        if len({a.id for a in self.actions}) != len(self.actions):
            raise ValueError("Duplicate action id")
        for name in self.probes:
            if not name.replace("_", "").isalnum():
                raise ValueError("Probe names must be alphanumeric/underscore")
        for a in self.actions:
            if a.target and a.target not in self.probes:
                raise ValueError(f"Unknown probe: {a.target}")
            if a.kind == "control" and self.domain != "realtime":
                raise ValueError("ControlFrame is only available in realtime profiles")
        return self

    @property
    def content_hash(self) -> str:
        return digest(self.model_dump(mode="json"))


class Observation(Schema):
    id: str = Field(default_factory=lambda: uid("obs"))
    runtime_id: str
    captured_at: float = Field(default_factory=time.monotonic)
    state: dict[str, Any]
    fingerprint: str


class Versions(Schema):
    goal: int
    control: int
    policy: int
    episode: str


class Action(Schema):
    id: str = Field(default_factory=lambda: uid("act"))
    session_id: str
    source: Literal["laya", "demo", "human"]
    template: ActionTemplate
    versions: Versions
    observation_id: str
    decision_id: str | None = None
    fingerprint: str
    target_uid: str | None = None
    deadline: float
    sequence: int = 0


class Decision(Schema):
    candidate_id: str
    provider: str
    probability: float = Field(ge=0, le=1)
    margin: float = Field(ge=0, le=1)
    latency_ms: float = Field(ge=0)
    routing_model: str = ""


class ExecutionResult(Schema):
    action_id: str
    status: Literal["executed", "rejected", "unknown", "failed"]
    code: str
    latency_ms: float = 0
    verification: Literal["passed", "failed", "unknown", "not_applicable"] = "unknown"


class Capabilities(Schema):
    screenshot: bool = True
    structured_ui: bool = True
    realtime_control: bool = False
    attached: bool = False


class SafetySettings(Schema):
    # These are USER permissions. Strategy packs cannot change them.
    allow_auto_click: bool = False
    allow_realtime: bool = False
    min_probability: float = Field(default=0.65, ge=0, le=1)
    min_margin: float = Field(default=0.1, ge=0, le=1)
