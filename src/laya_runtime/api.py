from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import shutil
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .models import Mode, Profile, RunState, SafetySettings, Schema, uid
from .policies import DemoPolicy, LocalLaya, Policy, RemoteLaya, sdk_available
from .profiles import Strategist, builtin_profiles
from .runtime import AndroidRuntime, BrowserRuntime, origin
from .service import AgentService
from .storage import EventStore


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("AGENT_DATA_DIR", ".data")))
    token: str = field(default_factory=lambda: os.getenv("AGENT_TOKEN") or secrets.token_urlsafe(32))
    policy_backend: str = field(default_factory=lambda: os.getenv("AGENT_POLICY", "local"))
    laya_model: str = field(default_factory=lambda: os.getenv("LAYA_MODEL", "multilingual"))
    laya_device: str = field(default_factory=lambda: os.getenv("LAYA_DEVICE", "cpu"))
    laya_url: str = field(default_factory=lambda: os.getenv("LAYA_BASE_URL", "http://127.0.0.1:8000"))
    executable: str | None = field(default_factory=lambda: os.getenv("BROWSER_EXECUTABLE") or shutil.which("chromium"))
    headless: bool = True
    testing: bool = False
    record_training: bool = field(default_factory=lambda: os.getenv("AGENT_RECORD_TRAINING") == "1")


def make_policy(settings: Settings) -> Policy:
    if settings.policy_backend == "local":
        return LocalLaya(settings.laya_model, settings.laya_device)
    if settings.policy_backend == "http":
        return RemoteLaya(settings.laya_url, settings.laya_model, os.getenv("LAYA_API_KEY", ""))
    if settings.policy_backend == "demo":
        return DemoPolicy()
    raise ValueError("AGENT_POLICY must be local, http or explicitly demo")


class TaskInput(Schema):
    instruction: str = Field(default="Fill the supplied fields", min_length=1, max_length=2000)
    facts: dict[str, str] = Field(default_factory=dict, max_length=50)

    @model_validator(mode="after")
    def bounded_facts(self):
        if any(len(k) > 100 or len(v) > 4000 for k, v in self.facts.items()) or sum(map(len, self.facts.values())) > 16000:
            raise ValueError("Fact budget exceeded")
        return self


class CreateSession(TaskInput):
    runtime_kind: Literal["browser", "android"] = "browser"
    profile_id: str = "form-demo"
    url: str | None = Field(default=None, max_length=2000)
    cdp_url: str | None = Field(default=None, max_length=200)
    allowed_origins: tuple[str, ...] = Field(default=(), max_length=12)
    serial: str | None = None
    mode: Mode = Mode.ASSIST
    safety: SafetySettings = Field(default_factory=SafetySettings)


class ResumeInput(Schema):
    mode: Mode | None = None


class ApprovalInput(Schema):
    action_id: str


class HumanInput(Schema):
    kind: Literal["click", "text", "key"]
    x: float | None = Field(default=None, ge=0, le=1)
    y: float | None = Field(default=None, ge=0, le=1)
    text: str | None = Field(default=None, max_length=4000)
    key: str | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def required_payload(self):
        if self.kind == "click" and (self.x is None or self.y is None):
            raise ValueError("Click requires x and y")
        if self.kind == "text" and self.text is None:
            raise ValueError("Text action requires text")
        if self.kind == "key" and self.key not in {"Enter", "Escape", "Tab", "Backspace", "ArrowUp", "ArrowDown"}:
            raise ValueError("Unsupported or missing key")
        return self


class CompileInput(Schema):
    profile_id: str
    goal: str = Field(max_length=2000)
    guide: str = Field(min_length=1, max_length=30000)


def create_app(settings: Settings | None = None, policy: Policy | None = None) -> FastAPI:
    settings = settings or Settings()
    store = EventStore(settings.data_dir / "service.db")
    service = AgentService(policy or make_policy(settings), store, settings.record_training)
    profiles = builtin_profiles()
    profile_dir = settings.data_dir / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    for path in profile_dir.glob("*.json"):
        try:
            pack = Profile.model_validate_json(path.read_text(encoding="utf-8"))
            profiles[pack.id] = pack
        except (ValueError, OSError):
            pass  # Invalid stored packs are never loaded into an active session.
    static = Path(str(files("laya_runtime").joinpath("static")))

    @asynccontextmanager
    async def lifespan(app):
        yield
        await service.close()

    app = FastAPI(title="try-laya — Laya-first Runtime", version="0.1.0", lifespan=lifespan)
    app.state.service, app.state.settings, app.state.profiles = service, settings, profiles
    app.add_middleware(TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]"] + (["testserver"] if settings.testing else []))

    def authorised(token: str) -> bool:
        return hmac.compare_digest(token.encode(), settings.token.encode())

    def valid_origin(headers) -> bool:
        value = headers.get("origin")
        if not value:
            return True  # CLI requests still require the bearer token.
        try:
            return origin(value) == origin("http://" + headers.get("host", ""))
        except ValueError:
            return False

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            if not authorised(token):
                return JSONResponse({"detail": "Missing or invalid bearer token"}, status_code=401)
            if not valid_origin(request.headers):
                return JSONResponse({"detail": "Cross-origin API access denied"}, status_code=403)
            try:
                if int(request.headers.get("content-length", "0")) > 1_000_000:
                    return JSONResponse({"detail": "Request too large"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.exception_handler(ValueError)
    async def invalid(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def session(sid):
        if sid not in service.sessions:
            raise HTTPException(404, "Unknown active session; automatic recovery/replay is disabled")
        return service.sessions[sid]

    @app.get("/")
    async def index():
        return FileResponse(static / "index.html")

    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/demo/form")
    async def form():
        return FileResponse(static / "form.html")

    @app.get("/demo/reaction")
    async def reaction():
        return FileResponse(static / "reaction.html")

    @app.get("/api/v1/health")
    async def health():
        return {"status": "ok", "provider": service.policy.name, "sdk_installed": sdk_available(),
                "simulation": service.policy.name.startswith("demo"),
                "ready": getattr(service.policy, "ready", None)}

    @app.post("/api/v1/policy/warmup")
    async def warmup():
        try:
            await service.policy.warmup()
        except Exception as exc:
            raise HTTPException(503, f"Policy warmup failed: {type(exc).__name__}; check installation, model cache and server") from exc
        return {"provider": service.policy.name, "warmed": True}

    @app.get("/api/v1/profiles/schema")
    async def profile_schema():
        return Profile.model_json_schema()

    @app.get("/api/v1/profiles")
    async def get_profiles():
        return [p.model_dump(mode="json") for p in profiles.values()]

    @app.post("/api/v1/profiles")
    async def import_profile(pack: Profile):
        # Profile IDs are schema-validated to a bounded path-safe alphabet.
        (profile_dir / f"{pack.id}.json").write_text(pack.model_dump_json(indent=2), encoding="utf-8")
        profiles[pack.id] = pack
        return {"id": pack.id, "hash": pack.content_hash, "activated": False}

    @app.post("/api/v1/strategist/compile")
    async def compile_profile(body: CompileInput):
        if not os.getenv("LLM_BASE_URL") or not os.getenv("LLM_MODEL"):
            raise HTTPException(503, "Configure LLM_BASE_URL and LLM_MODEL, or import an external JSON profile")
        if body.profile_id not in profiles:
            raise HTTPException(404, "Unknown starting profile")
        helper = Strategist(os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"], os.getenv("LLM_API_KEY", ""))
        try:
            draft = await helper.compile(body.guide, body.goal, profiles[body.profile_id])
        except Exception as exc:
            raise HTTPException(502, f"Strategist failed or returned an invalid profile: {type(exc).__name__}") from exc
        return {"draft": draft.model_dump(mode="json"), "activated": False}

    @app.get("/api/v1/sessions")
    async def list_sessions():
        return [s.public() for s in service.sessions.values()]

    @app.post("/api/v1/sessions")
    async def create_session(body: CreateSession, request: Request):
        if body.profile_id not in profiles:
            raise HTTPException(404, "Unknown profile")
        if len(service.sessions) >= 4:
            raise HTTPException(409, "This single-process MVP supports at most four active sessions")
        pack = profiles[body.profile_id]
        runtime = None
        try:
            if body.runtime_kind == "browser":
                route = "reaction" if pack.domain == "realtime" else "form"
                url = body.url
                fixture = (static / ("reaction.html" if route == "reaction" else "form.html")).read_text(encoding="utf-8") if not url else None
                runtime = await BrowserRuntime.create(url, headless=settings.headless,
                    executable_path=settings.executable, cdp_url=body.cdp_url,
                    allowed_origins=body.allowed_origins, inline_html=fixture)
            else:
                if not body.serial:
                    raise ValueError("Select a concrete ADB serial; no implicit device selection")
                runtime = AndroidRuntime(body.serial)
                await runtime.health()
            s = await service.add(runtime, pack, body.instruction, body.facts, body.mode, body.safety)
        except Exception as exc:
            if runtime:
                await runtime.close()
            raise HTTPException(400, f"Runtime creation failed: {type(exc).__name__}: {exc}") from exc
        return s.public()

    @app.get("/api/v1/sessions/{sid}")
    async def get_session(sid: str):
        return session(sid).public()

    @app.delete("/api/v1/sessions/{sid}")
    async def close_session(sid: str):
        await service.remove(session(sid))
        return {"closed": True, "events_retained": True}

    @app.post("/api/v1/sessions/{sid}/resume")
    async def resume(sid: str, body: ResumeInput):
        s = session(sid)
        await service.resume(s, body.mode)
        return s.public()

    @app.post("/api/v1/sessions/{sid}/pause")
    async def pause(sid: str):
        s = session(sid)
        await service.interrupt(s)
        return s.public()

    @app.post("/api/v1/sessions/{sid}/stop")
    async def stop(sid: str):
        s = session(sid)
        await service.interrupt(s, RunState.STOPPED)
        return s.public()

    @app.post("/api/v1/sessions/{sid}/episodes")
    async def episode(sid: str):
        s = session(sid)
        await service.new_episode(s)
        return s.public()

    @app.post("/api/v1/sessions/{sid}/goal")
    async def update_goal(sid: str, body: TaskInput):
        s = session(sid)
        await service.update_goal(s, body.instruction, body.facts)
        return s.public()

    @app.post("/api/v1/sessions/{sid}/profile")
    async def activate(sid: str, pack: Profile):
        s = session(sid)
        await service.activate_profile(s, pack)
        return s.public()

    @app.post("/api/v1/sessions/{sid}/approve")
    async def approve(sid: str, body: ApprovalInput):
        return (await service.approve(session(sid), body.action_id)).model_dump()

    @app.post("/api/v1/sessions/{sid}/human")
    async def manual(sid: str, body: HumanInput):
        s = session(sid)
        await service.manual(s, body.kind, body.model_dump(exclude_none=True))
        return s.public()

    @app.get("/api/v1/sessions/{sid}/observation")
    async def observation(sid: str):
        s = session(sid)
        async with s.gate:
            return (await service.observe(s)).model_dump()

    @app.get("/api/v1/sessions/{sid}/screenshot")
    async def screenshot(sid: str):
        return Response(await session(sid).runtime.screenshot(), media_type="image/png")

    @app.post("/api/v1/sessions/{sid}/artifacts")
    async def artifact(sid: str):
        s = session(sid)
        png = await s.runtime.screenshot()
        artifact_id = uid("frame")
        folder = settings.data_dir / "artifacts" / s.id
        folder.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread((folder / f"{artifact_id}.png").write_bytes, png)
        metadata = {"artifact_id": artifact_id, "sha256": hashlib.sha256(png).hexdigest(), "bytes": len(png)}
        await store.emit(s.id, "ARTIFACT_SAVED", metadata)
        return metadata

    @app.get("/api/v1/sessions/{sid}/events")
    async def events(sid: str, after: int = 0, limit: int = 200):
        return await store.events(sid, after, limit)

    @app.websocket("/api/v1/ws/{sid}")
    async def stream(ws: WebSocket, sid: str):
        if not valid_origin(ws.headers):
            await ws.close(code=1008)
            return
        await ws.accept()
        queue = None
        try:
            auth_message = await asyncio.wait_for(ws.receive_json(), timeout=5)
            if not authorised(str(auth_message.get("token", ""))) or sid not in service.sessions:
                await ws.close(code=1008)
                return
            queue = store.subscribe(sid)
            await ws.send_json({"type": "STREAM_READY"})
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    await ws.send_json(event)
                except asyncio.TimeoutError:
                    await ws.send_json({"type": "HEARTBEAT"})
        except (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError, ValueError):
            pass
        finally:
            if queue:
                store.unsubscribe(sid, queue)

    return app
