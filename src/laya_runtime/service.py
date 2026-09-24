from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from .control import MotorLease, Rejected, requires_approval
from .models import (Action, ExecutionResult, Mode, Observation, Profile, RunState,
                     SafetySettings, Versions, uid)
from .policies import Policy, PolicyError
from .profiles import ContextError, build_request, candidates
from .runtime import RuntimeAdapter
from .storage import EventStore


@dataclass
class Session:
    runtime: RuntimeAdapter
    profile: Profile
    instruction: str
    facts: dict[str, str]
    mode: Mode = Mode.ASSIST
    safety: SafetySettings = field(default_factory=SafetySettings)
    id: str = field(default_factory=lambda: uid("session"))
    episode: str = field(default_factory=lambda: uid("episode"))
    state: RunState = RunState.IDLE
    owner: str = "human"
    goal_version: int = 1
    control_epoch: int = 0
    policy_version: int = 1
    state_version: int = 0
    steps: int = 0
    sequence: int = 0
    waits: int = 0
    attempts: int = 0
    pending: Action | None = None
    observation: Observation | None = None
    last_result: ExecutionResult | None = None
    runner: asyncio.Task | None = None
    gate: asyncio.Lock = field(default_factory=asyncio.Lock)
    step_gate: asyncio.Lock = field(default_factory=asyncio.Lock)
    motor: MotorLease = field(init=False)

    def __post_init__(self):
        self.motor = MotorLease(self.runtime, self.gate)

    def versions(self) -> Versions:
        return Versions(goal=self.goal_version, control=self.control_epoch,
                        policy=self.policy_version, episode=self.episode)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "runtime_id": self.runtime.id, "profile_id": self.profile.id,
                "profile_hash": self.profile.content_hash, "episode": self.episode,
                "mode": self.mode.value, "state": self.state.value, "owner": self.owner,
                "versions": self.versions().model_dump(), "state_version": self.state_version,
                "steps": self.steps, "instruction": self.instruction,
                "fact_keys": list(self.facts),  # no plaintext credentials/form facts
                "capabilities": self.runtime.capabilities.model_dump(),
                "pending": self.pending.model_dump(mode="json") if self.pending else None,
                "last_result": self.last_result.model_dump(mode="json") if self.last_result else None}


class AgentService:
    def __init__(self, policy: Policy, store: EventStore, record_training: bool = False):
        self.policy, self.store = policy, store
        self.record_training = record_training
        self.sessions: dict[str, Session] = {}
        self._runtime_owners: set[str] = set()

    async def add(self, runtime: RuntimeAdapter, profile: Profile, instruction: str,
                  facts: dict[str, str], mode: Mode = Mode.ASSIST,
                  safety: SafetySettings | None = None) -> Session:
        if runtime.id in self._runtime_owners:
            raise ValueError("A runtime may have only one controlling session")
        if profile.domain == "realtime" and not runtime.capabilities.realtime_control:
            raise ValueError("Realtime profile requires a persistent-control adapter, not basic ADB")
        if runtime.capabilities.attached and mode == Mode.AUTO:
            raise ValueError("Attached browser is assist/manual only until external-input tracking is implemented")
        s = Session(runtime, profile.model_copy(deep=True), instruction, dict(facts), mode, safety or SafetySettings())
        self.sessions[s.id] = s
        self._runtime_owners.add(runtime.id)
        await self.store.emit(s.id, "SESSION_CREATED", {"episode": s.episode, "profile_hash": profile.content_hash,
                                                       "provider": self.policy.name})
        return s

    async def observe(self, s: Session) -> Observation:
        # Caller holds s.gate. Screenshot refreshes never increment state_version.
        obs = await s.runtime.observe(s.profile)
        if not s.observation or s.observation.fingerprint != obs.fingerprint:
            s.state_version += 1
        s.observation = obs
        return obs

    def _valid(self, s: Session, action: Action, check_deadline: bool = True) -> None:
        if action.versions != s.versions():
            raise Rejected("STALE_VERSIONS")
        if s.owner != "agent" or s.mode == Mode.MANUAL:
            raise Rejected("HUMAN_OWNS_CONTROL")
        if s.state != RunState.RUNNING:
            raise Rejected("SESSION_NOT_RUNNING")
        if check_deadline and time.monotonic() > action.deadline:
            raise Rejected("OBSERVATION_DEADLINE_EXCEEDED")

    async def _needs_context(self, s: Session, code: str, versions: Versions) -> None:
        if s.versions() != versions or s.owner != "agent":
            return
        s.state = RunState.NEEDS_CONTEXT
        s.pending = None
        async with s.gate:
            await s.motor.neutral()
        await self.store.emit(s.id, "CONTEXT_REQUESTED", {"code": code, "episode": s.episode,
            "profile_hash": s.profile.content_hash, "llm_on_action_path": False})

    async def step(self, s: Session) -> ExecutionResult | None:
        if s.step_gate.locked():
            return None  # at most one inference per session; never build an old-action queue
        async with s.step_gate:
            if s.state != RunState.RUNNING or s.owner != "agent" or s.mode == Mode.MANUAL:
                return None
            versions = s.versions()
            s.attempts += 1
            if s.attempts > s.profile.max_steps * 3:
                await self._needs_context(s, "DECISION_ATTEMPT_BUDGET_EXHAUSTED", versions)
                return None
            if s.steps >= s.profile.max_steps:
                await self._needs_context(s, "STEP_BUDGET_EXHAUSTED", versions)
                return None
            if s.motor.failure:
                await self._needs_context(s, "MOTOR_WATCHDOG_FAILURE", versions)
                return None
            try:
                async with s.gate:
                    obs = await self.observe(s)
                if versions != s.versions():
                    return None
                if s.profile.done_when and all(c.matches(obs.state) for c in s.profile.done_when):
                    s.state = RunState.COMPLETED
                    async with s.gate:
                        await s.motor.neutral()
                    await self.store.emit(s.id, "EPISODE_COMPLETED", {"episode": s.episode, "steps": s.steps})
                    return None
                options = candidates(s.profile, obs, s.facts)
                if not options:
                    await self._needs_context(s, "NO_GROUNDED_CANDIDATES", versions)
                    return None
                state, questions = build_request(s.profile, s.instruction, obs, options)
                deadline = obs.captured_at + s.profile.max_observation_age_ms / 1000
                # Model inference NEVER holds the runtime execution gate.
                decision = await self.policy.predict(state, questions, max(.001, deadline - time.monotonic()))
                if versions != s.versions() or s.state != RunState.RUNNING or s.owner != "agent":
                    await self.store.emit(s.id, "DECISION_DROPPED", {"code": "CONTROL_OR_CONTEXT_CHANGED"})
                    return None
                decision_id = uid("decision")
                trace = {"context": state, "candidates": [a.model_dump(mode="json") for a in options]} if self.record_training else {}
                await self.store.emit(s.id, "AGENT_DECISION", {
                    **decision.model_dump(), "decision_id": decision_id, "observation_id": obs.id,
                    "profile_hash": s.profile.content_hash, "episode": s.episode,
                    "candidate_ids": [a.id for a in options], **trace}, correlation_id=s.episode)
                if decision.candidate_id == "__escalate__":
                    await self._needs_context(s, "LAYA_REQUESTED_CONTEXT", versions)
                    return None
                if decision.probability < s.safety.min_probability or decision.margin < s.safety.min_margin:
                    await self._needs_context(s, "UNCERTAIN_DECISION", versions)
                    return None
                if decision.candidate_id == "__wait__":
                    s.waits += 1
                    if s.waits >= 5:
                        await self._needs_context(s, "REPEATED_WAIT", versions)
                    return None
                selected = next((a for a in options if a.id == decision.candidate_id), None)
                if not selected:
                    await self._needs_context(s, "INVALID_CANDIDATE", versions)
                    return None
                s.waits = 0
                s.sequence += 1
                action = Action(session_id=s.id, source="demo" if self.policy.name.startswith("demo") else "laya",
                    template=selected.model_copy(deep=True), versions=versions, observation_id=obs.id, decision_id=decision_id,
                    fingerprint=obs.fingerprint,
                    target_uid=obs.state.get("fields", {}).get(selected.target, {}).get("uid"),
                    deadline=deadline, sequence=s.sequence)
                self._valid(s, action)
                needs_approval = s.mode == Mode.ASSIST or requires_approval(action, s.safety, obs.state)
                if needs_approval:
                    s.pending, s.state = action, RunState.AWAITING_APPROVAL
                    async with s.gate:
                        await s.motor.neutral()
                    await self.store.emit(s.id, "ACTION_PROPOSED", {"action_id": action.id, "kind": selected.kind,
                        "target": selected.target, "versions": versions.model_dump()})
                    return None
                return await self.dispatch(s, action)
            except (PolicyError, ContextError) as exc:
                await self._needs_context(s, str(exc), versions)
                return None
            except Rejected as exc:
                await self.store.emit(s.id, "ACTION_REJECTED", {"code": str(exc)})
                if str(exc) in {"SENSITIVE_FIELD_REQUIRES_MANUAL_INPUT", "REALTIME_PERMISSION_REQUIRED",
                                "RUNTIME_HAS_NO_PERSISTENT_CONTROL"}:
                    await self._needs_context(s, str(exc), versions)
                return None
            except Exception as exc:
                # Do not echo raw driver/HTTP errors containing tokens or field values.
                await self._needs_context(s, f"RUNTIME_OR_POLICY_ERROR:{type(exc).__name__}", versions)
                return None

    async def dispatch(self, s: Session, action: Action, *, approved: bool = False) -> ExecutionResult:
        start = time.monotonic()
        admitted = False
        try:
            async with s.gate:
                self._valid(s, action, check_deadline=not approved)
                fresh = await self.observe(s)
                self._valid(s, action, check_deadline=not approved)
                if action.template.kind != "control" and fresh.fingerprint != action.fingerprint:
                    raise Rejected("STALE_STATE_OR_TARGET")
                if action.template.target:
                    field = fresh.state.get("fields", {}).get(action.template.target, {})
                    if field.get("uid") != action.target_uid or not field.get("present"):
                        raise Rejected("STALE_TARGET_BINDING")
                if not all(c.matches(fresh.state) for c in action.template.when):
                    raise Rejected("ACTION_PRECONDITION_FAILED")
                needs_approval = requires_approval(action, s.safety, fresh.state)
                if needs_approval and not approved:
                    raise Rejected("APPROVAL_REQUIRED")
                cached = await self.store.admit(action.id, s.id)
                if cached is not None:
                    return cached
                admitted = True
                # A human can invalidate ownership while the durable journal is committing.
                self._valid(s, action, check_deadline=not approved)
                execution_action = action
                if approved:
                    # Approval renews only freshness after an identical state/target is verified.
                    # It never changes the effect, target, values, or context versions.
                    execution_action = action.model_copy(update={
                        "deadline": fresh.captured_at + s.profile.max_observation_age_ms / 1000})
                if action.template.kind == "control":
                    await s.motor.apply(execution_action)
                else:
                    await s.runtime.execute(execution_action, s.facts)
                s.steps += 1
                verification = "not_applicable" if action.template.kind == "control" else "unknown"
                if action.template.expect:
                    after = await self.observe(s)
                    if s.versions() == action.versions:
                        verification = "passed" if all(c.matches(after.state) for c in action.template.expect) else "failed"
                result = ExecutionResult(action_id=action.id, status="executed", code="DISPATCHED",
                    latency_ms=(time.monotonic()-start)*1000, verification=verification)
        except Rejected as exc:
            result = ExecutionResult(action_id=action.id, status="rejected", code=str(exc))
        except Exception as exc:
            # An input may already have reached the app even if its acknowledgement timed out.
            result = ExecutionResult(action_id=action.id, status="unknown", code=f"EXECUTION_UNCERTAIN:{type(exc).__name__}")
        if admitted:
            await self.store.finish(result)
        s.last_result = result
        await self.store.emit(s.id, "ACTION_RESULT", {**result.model_dump(), "decision_id": action.decision_id},
                              correlation_id=s.episode, causation_id=action.id)
        if result.status == "unknown" or result.verification == "failed":
            await self._needs_context(s, "VERIFY_OR_EXECUTION_UNCERTAIN_NO_AUTO_RETRY", action.versions)
        return result

    async def approve(self, s: Session, action_id: str) -> ExecutionResult:
        action = s.pending
        if not action or action.id != action_id:
            raise ValueError("Approval is absent, consumed, or belongs to a different action")
        s.pending = None  # single-use before any await
        if time.monotonic() > action.deadline + 60 or action.versions != s.versions():
            s.state = RunState.PAUSED
            raise ValueError("Approval expired or context changed")
        if action.template.kind == "control":
            s.state = RunState.PAUSED
            raise ValueError("Realtime frames cannot wait for human approval; explicitly use auto mode and permission")
        s.state = RunState.RUNNING
        # A fresh observation must have the SAME fingerprint and target, even after human approval.
        result = await self.dispatch(s, action, approved=True)
        if s.owner == "agent" and s.state == RunState.RUNNING:
            self._ensure_runner(s)
        return result

    async def interrupt(self, s: Session, state: RunState = RunState.PAUSED) -> None:
        # Epoch/ownership update before awaiting anything: late inference loses eligibility now.
        s.control_epoch += 1
        s.owner, s.state, s.pending = "human", state, None
        async with s.gate:
            await s.motor.neutral()
        await self.store.emit(s.id, "CONTROL_TAKEOVER", {"epoch": s.control_epoch, "state": state.value})

    async def manual(self, s: Session, kind: str, payload: dict) -> None:
        await self.interrupt(s)
        async with s.gate:
            await s.runtime.manual(kind, payload)
        await self.store.emit(s.id, "HUMAN_ACTION", {"kind": kind, "epoch": s.control_epoch})

    async def update_goal(self, s: Session, instruction: str, facts: dict[str, str]) -> None:
        s.goal_version += 1
        s.instruction, s.facts = instruction, dict(facts)
        await self.interrupt(s)
        await self.store.emit(s.id, "GOAL_UPDATED", {"goal_version": s.goal_version})

    async def activate_profile(self, s: Session, profile: Profile) -> None:
        if profile.domain == "realtime" and not s.runtime.capabilities.realtime_control:
            raise ValueError("Runtime cannot execute realtime profile")
        s.policy_version += 1
        s.profile = profile.model_copy(deep=True)
        await self.interrupt(s)
        await self.store.emit(s.id, "PROFILE_ACTIVATED", {"hash": profile.content_hash, "policy_version": s.policy_version})

    async def new_episode(self, s: Session) -> None:
        await self.interrupt(s)
        s.episode = uid("episode")
        s.steps, s.waits, s.attempts = 0, 0, 0
        s.last_result = None
        await self.store.emit(s.id, "EPISODE_CREATED", {"episode": s.episode, "profile_hash": s.profile.content_hash})

    async def resume(self, s: Session, mode: Mode | None = None) -> None:
        mode = mode or s.mode
        if s.runtime.capabilities.attached and mode == Mode.AUTO:
            raise ValueError("Attached browser auto mode is not supported without external-input monitoring")
        if mode == Mode.MANUAL:
            s.mode = mode
            await self.interrupt(s)
            return
        if s.profile.domain == "realtime" and (mode != Mode.AUTO or not s.safety.allow_realtime):
            raise ValueError("Realtime requires auto mode AND explicit realtime permission")
        if s.state in {RunState.COMPLETED, RunState.STOPPED}:
            raise ValueError("Create a new episode before resuming a completed/stopped episode")
        if s.pending:
            raise ValueError("Approve or take control to discard the pending action first")
        s.mode, s.owner, s.state = mode, "agent", RunState.RUNNING
        s.control_epoch += 1
        await self.store.emit(s.id, "AGENT_RESUMED", {"mode": s.mode.value, "epoch": s.control_epoch})
        self._ensure_runner(s)

    def _ensure_runner(self, s: Session) -> None:
        if not s.runner or s.runner.done():
            s.runner = asyncio.create_task(self._run(s))

    async def _run(self, s: Session) -> None:
        while s.state == RunState.RUNNING and s.owner == "agent":
            await self.step(s)
            await asyncio.sleep(s.profile.tick_ms / 1000)

    async def remove(self, s: Session) -> None:
        await self.interrupt(s, RunState.STOPPED)
        if s.runner:
            s.runner.cancel()
            await asyncio.gather(s.runner, return_exceptions=True)
        await s.runtime.close()
        self._runtime_owners.discard(s.runtime.id)
        self.sessions.pop(s.id, None)
        await self.store.emit(s.id, "SESSION_CLOSED")

    async def close(self) -> None:
        for s in list(self.sessions.values()):
            await self.remove(s)
        await self.policy.close()
        await self.store.close()
