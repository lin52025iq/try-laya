from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import re
import secrets
import shutil
import struct
import time
import xml.etree.ElementTree as ET
from typing import Any, Protocol
from urllib.parse import urlsplit

from .models import Action, Capabilities, Controls, Observation, Profile, digest, uid


class RuntimeFault(RuntimeError):
    pass


def origin(url: str) -> str:
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("Only explicit http(s) URLs without embedded credentials are supported")
    host = f"[{p.hostname}]" if ":" in p.hostname else p.hostname
    port = p.port or (443 if p.scheme == "https" else 80)
    return f"{p.scheme}://{host}:{port}"


class RuntimeAdapter(Protocol):
    id: str
    capabilities: Capabilities
    async def observe(self, profile: Profile) -> Observation: ...
    async def execute(self, action: Action, facts: dict[str, str]) -> None: ...
    async def set_controls(self, controls: Controls) -> None: ...
    async def screenshot(self) -> bytes: ...
    async def manual(self, kind: str, payload: dict) -> None: ...
    async def close(self) -> None: ...


_ELEMENT = r"""el => {
  let s = window.__tryLayaNodes;
  if (!s) s = window.__tryLayaNodes = {doc: Date.now().toString(36)+Math.random().toString(36), next: 0, ids: new WeakMap()};
  if (!s.ids.has(el)) s.ids.set(el, s.doc + ':' + (++s.next));
  const r = el.getBoundingClientRect();
  const sensitive = el.type === 'password' || /password|one-time-code/.test(el.autocomplete || '');
  const input = /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName);
  return {uid:s.ids.get(el), document:s.doc, present:r.width>0 && r.height>0,
    tag:el.tagName.toLowerCase(), label:(el.getAttribute('aria-label') || el.labels?.[0]?.textContent || '').trim().slice(0,80),
    text:input ? '' : (el.textContent||'').trim().slice(0,120),
    empty:input ? !el.value : false, disabled:!!el.disabled,
    sensitive, focused:el===document.activeElement, checked:!!el.checked,
    _value: input ? el.value : '', _connected:el.isConnected};
}"""


class BrowserRuntime:
    def __init__(self):
        self.id = uid("browser")
        self.capabilities = Capabilities(realtime_control=True)
        self._pw = self._browser = self.page = self._context = None
        self._handles: dict[str, Any] = {}
        self._held: set[str] = set()
        self._salt = secrets.token_bytes(32)
        self._allowed: set[str] = set()
        self._cursor = [300.0, 250.0]
        self._local_fixture = False
        self._closed = False

    @classmethod
    async def create(cls, url: str | None, *, headless: bool = True, executable_path: str | None = None,
                     cdp_url: str | None = None, allowed_origins: tuple[str, ...] = (),
                     inline_html: str | None = None) -> BrowserRuntime:
        from playwright.async_api import async_playwright
        obj = cls()
        if url is None and inline_html is None:
            raise ValueError("Supply an authorised URL or an internal local fixture")
        obj._local_fixture = inline_html is not None
        obj._allowed = ({origin(url)} if url else set()) | {origin(u) for u in allowed_origins}
        obj._pw = await async_playwright().start()
        try:
            if cdp_url:
                if urlsplit(cdp_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
                    raise RuntimeFault("Only an explicitly local CDP endpoint is supported")
                obj._browser = await obj._pw.chromium.connect_over_cdp(cdp_url, timeout=5000)
                obj._context = obj._browser.contexts[0]
                # Create an owned tab; do not attach input/routing hooks to the user's other tabs.
                obj.page = await obj._context.new_page()
                obj.capabilities = Capabilities(realtime_control=False, attached=True)
            else:
                obj._browser = await obj._pw.chromium.launch(headless=headless, executable_path=executable_path)
                obj._context = await obj._browser.new_context(viewport={"width": 1100, "height": 700},
                    accept_downloads=False, service_workers="block")
                obj.page = await obj._context.new_page()
            obj.page.set_default_timeout(1200)
            async def route(request_route):
                try:
                    permitted = origin(request_route.request.url) in obj._allowed
                except ValueError:
                    permitted = False
                await (request_route.continue_() if permitted else request_route.abort())
            await obj.page.route("**/*", route)
            # No network WebSocket access by page content in this first security profile.
            await obj.page.route_web_socket("**/*", lambda ws: ws.close())
            if inline_html is not None:
                await obj.page.set_content(inline_html, wait_until="domcontentloaded")
            else:
                await obj.page.goto(url, wait_until="domcontentloaded", timeout=15000)
            return obj
        except BaseException:
            await obj.close()
            raise

    async def observe(self, profile: Profile) -> Observation:
        started = time.monotonic()
        if not (self._local_fixture and self.page.url == "about:blank"):
            if origin(self.page.url) not in self._allowed:
                raise RuntimeFault("Page left the authorised origin")
        fields, private_values, fresh_handles = {}, {}, {}
        for name, probe in profile.probes.items():
            locator = self.page.locator(probe.selector)
            count = await locator.count()
            if count != 1:
                fields[name] = {"present": False, "count": count}
                continue
            handle = await locator.element_handle()
            if handle is None:
                fields[name] = {"present": False, "count": 0}
                continue
            data = await handle.evaluate(_ELEMENT)
            raw = data.pop("_value", "")
            data.pop("_connected", None)
            # Changes to a filled input must invalidate a pending submit, even though its
            # contents must not be sent to Laya or written into the event log.
            private_values[name] = hmac.new(self._salt, raw.encode(), hashlib.sha256).hexdigest()
            data["count"] = 1
            if probe.kind == "number":
                try:
                    value = float(data["text"])
                    if math.isfinite(value):
                        data["number"] = value
                except ValueError:
                    pass
            fields[name] = data
            fresh_handles[data["uid"]] = handle
        for handle in self._handles.values():
            await handle.dispose()
        self._handles = fresh_handles
        parsed_url = urlsplit(self.page.url)
        safe_url = parsed_url._replace(query="", fragment="").geturl()
        private_values["__url"] = hmac.new(self._salt, self.page.url.encode(), hashlib.sha256).hexdigest()
        state = {"url": safe_url, "title": (await self.page.title())[:100], "fields": fields}
        return Observation(runtime_id=self.id, state=state, captured_at=started,
                           fingerprint=digest({"state": state, "values": private_values}))

    async def execute(self, action: Action, facts: dict[str, str]) -> None:
        a = action.template
        timeout_ms = max(1, min(1200, int((action.deadline - time.monotonic()) * 1000)))
        if time.monotonic() >= action.deadline:
            raise RuntimeFault("Action expired before browser dispatch")
        handle = self._handles.get(action.target_uid) if action.target_uid else None
        if a.target and (handle is None or not await handle.evaluate("el => el.isConnected")):
            raise RuntimeFault("Target detached or belongs to a different observation")
        # Use the observed ElementHandle, not a locator that can silently retarget a replacement.
        if a.kind == "click":
            await handle.click(timeout=timeout_ms)
        elif a.kind == "fill":
            await handle.fill(facts[a.value_ref], timeout=timeout_ms)
            if await handle.input_value() != facts[a.value_ref]:
                raise RuntimeFault("Input did not retain the requested value; do not blindly retry")
        elif a.kind == "select":
            await handle.select_option(facts[a.value_ref], timeout=timeout_ms)
        elif a.kind == "key":
            await self.page.keyboard.press(a.key)
        else:
            raise RuntimeFault(f"Unsupported browser transactional action: {a.kind}")

    async def set_controls(self, controls: Controls) -> None:
        if not self.capabilities.realtime_control:
            if controls.keys or controls.dx or controls.dy:
                raise RuntimeFault("Runtime does not advertise persistent control support")
            return  # Neutral is a safe no-op for an attached, non-realtime tab.
        desired = set(controls.keys)
        for key in self._held - desired:
            await self.page.keyboard.up(key)
            self._held.discard(key)
        for key in desired - self._held:
            # Track attempted key-down before awaiting its acknowledgement: the device may
            # receive it even if the driver times out, so neutral must still send key-up.
            self._held.add(key)
            await self.page.keyboard.down(key)
        if controls.dx or controls.dy:
            self._cursor[0] = min(1099, max(0, self._cursor[0] + controls.dx))
            self._cursor[1] = min(699, max(0, self._cursor[1] + controls.dy))
            await self.page.mouse.move(*self._cursor)

    async def screenshot(self) -> bytes:
        return await self.page.screenshot(type="png", timeout=3000)

    async def manual(self, kind: str, payload: dict) -> None:
        if kind == "click":
            x, y = float(payload["x"]), float(payload["y"])
            if not (0 <= x <= 1 and 0 <= y <= 1):
                raise ValueError("Coordinates must be normalised to [0,1]")
            viewport = self.page.viewport_size or await self.page.evaluate("({width:innerWidth,height:innerHeight})")
            await self.page.mouse.click(x * viewport["width"], y * viewport["height"])
        elif kind == "text":
            text = str(payload["text"])
            if len(text) > 4000:
                raise ValueError("Text too large")
            await self.page.keyboard.insert_text(text)
        elif kind == "key" and payload.get("key") in {"Enter", "Escape", "Tab", "Backspace", "ArrowUp", "ArrowDown"}:
            await self.page.keyboard.press(payload["key"])
        else:
            raise ValueError("Unsupported manual action")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.page and not self.page.is_closed():
                if self.capabilities.realtime_control:
                    await self.set_controls(Controls())
                if self.capabilities.attached:
                    await self.page.close()  # only the tab created by this runtime
            if self._browser and not self.capabilities.attached:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()


class AndroidRuntime:
    """Conservative ADB adapter, NOT a high-rate multitouch/FPS driver."""
    capabilities = Capabilities(realtime_control=False)

    def __init__(self, serial: str, adb: str = "adb"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,100}", serial):
            raise ValueError("Invalid ADB serial")
        self.id, self.serial, self.adb = uid("android"), serial, adb
        self._targets: dict[str, dict] = {}
        self._salt = secrets.token_bytes(32)

    async def _run(self, *args: str, timeout: float = 5) -> bytes:
        proc = await asyncio.create_subprocess_exec(self.adb, "-s", self.serial, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
            await proc.communicate()
            raise
        if proc.returncode:
            raise RuntimeFault(f"ADB command failed (exit {proc.returncode}); device may be disconnected")
        if len(out) > 20_000_000:
            raise RuntimeFault("Oversized ADB response")
        return out

    async def health(self) -> None:
        if not shutil.which(self.adb):
            raise RuntimeFault("ADB is not installed")
        if (await self._run("get-state")).strip() != b"device":
            raise RuntimeFault("ADB device is not authorised/ready")

    @staticmethod
    def parse_tree(raw: bytes) -> list[dict]:
        if len(raw) > 1_000_000 or b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
            raise RuntimeFault("Unsafe or oversized UI tree")
        start, end = raw.find(b"<hierarchy"), raw.rfind(b"</hierarchy>")
        if start < 0 or end < 0:
            raise RuntimeFault("ADB UI tree unavailable; visual perception is required")
        root = ET.fromstring(raw[start:end + len(b"</hierarchy>")])
        return [dict(node.attrib) for node in root.iter("node")]

    async def observe(self, profile: Profile) -> Observation:
        started = time.monotonic()
        nodes = self.parse_tree(await self._run("shell", "uiautomator", "dump", "/dev/tty"))
        fields, self._targets = {}, {}
        for name, probe in profile.probes.items():
            key, sep, value = probe.selector.partition("=")
            if not sep or key not in {"resource-id", "text", "content-desc"}:
                raise RuntimeFault("ADB probes use resource-id=..., text=... or content-desc=...")
            matches = [n for n in nodes if n.get(key) == value]
            if len(matches) != 1:
                fields[name] = {"present": False, "count": len(matches)}
                continue
            node = matches[0]
            sensitive = node.get("password") == "true"
            editable = "EditText" in node.get("class", "")
            target_id = hmac.new(self._salt, digest(node).encode(), hashlib.sha256).hexdigest()
            data = {"present": True, "count": 1, "uid": target_id,
                    "text": "" if sensitive or editable else node.get("text", "")[:120],
                    "empty": not node.get("text"), "sensitive": sensitive,
                    "focused": node.get("focused") == "true", "disabled": node.get("enabled") == "false"}
            if probe.kind == "number":
                try:
                    n = float(data["text"])
                    if math.isfinite(n):
                        data["number"] = n
                except ValueError:
                    pass
            fields[name], self._targets[target_id] = data, node
        state = {"fields": fields}
        return Observation(runtime_id=self.id, captured_at=started, state=state, fingerprint=digest(state))

    async def execute(self, action: Action, facts: dict[str, str]) -> None:
        a = action.template
        target = self._targets.get(action.target_uid)
        if a.kind in {"tap", "click"}:
            if not target:
                raise RuntimeFault("Stale ADB target")
            bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", target.get("bounds", ""))
            if not bounds:
                raise RuntimeFault("Invalid ADB bounds")
            x1, y1, x2, y2 = map(int, bounds.groups())
            if x2 <= x1 or y2 <= y1:
                raise RuntimeFault("Empty ADB bounds")
            await self._run("shell", "input", "tap", str((x1+x2)//2), str((y1+y2)//2))
        elif a.kind == "fill":
            if not target or target.get("focused") != "true":
                raise RuntimeFault("ADB input requires a separately focused target; tap then re-observe")
            await self._input_text(facts[a.value_ref])
        elif a.kind == "key":
            keys = {"Enter": "66", "Tab": "61", "Escape": "111", "Backspace": "67", "ArrowUp": "19", "ArrowDown": "20"}
            await self._run("shell", "input", "keyevent", keys[a.key])
        else:
            raise RuntimeFault("ADB action unsupported; ControlFrame requires a separate device-side bridge")

    async def _input_text(self, text: str) -> None:
        # adb shell ultimately invokes a device shell. A host argv array alone is NOT enough.
        if not re.fullmatch(r"[A-Za-z0-9 @._+-]{1,2000}", text):
            raise RuntimeFault("Basic ADB text supports a restricted ASCII set; Unicode needs an IME adapter")
        await self._run("shell", "input", "text", text.replace(" ", "%s"))

    async def screenshot(self) -> bytes:
        return await self._run("exec-out", "screencap", "-p")

    async def manual(self, kind: str, payload: dict) -> None:
        if kind == "click":
            x, y = float(payload["x"]), float(payload["y"])
            if not (0 <= x <= 1 and 0 <= y <= 1):
                raise ValueError("Coordinates must be in [0,1]")
            png = await self.screenshot()
            if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) < 24:
                raise RuntimeFault("Invalid screenshot")
            width, height = struct.unpack(">II", png[16:24])
            await self._run("shell", "input", "tap", str(min(width-1, int(x*width))), str(min(height-1, int(y*height))))
        elif kind == "text":
            await self._input_text(str(payload["text"]))
        else:
            raise RuntimeFault("Unsupported manual ADB action")

    async def set_controls(self, controls: Controls) -> None:
        if controls.keys or controls.dx or controls.dy:
            raise RuntimeFault("Basic ADB cannot provide leased simultaneous realtime controls")

    async def close(self) -> None:
        pass  # User owns the device. Never stop its emulator or kill the shared ADB server.
