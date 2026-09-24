"""Real Chromium UI + runtime, using an in-process ASGI transport.

The development container administratively blocks Chromium network navigation.
This bridge is ONLY a test harness. It does not bypass network policy or claim to
validate network navigation. The actual application uses fetch/WebSocket normally.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import tempfile
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from laya_runtime.api import Settings, create_app
from laya_runtime.policies import DemoPolicy


async def run(output: Path):
    static = Path(__file__).parents[1] / "src/laya_runtime/static"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        app = create_app(Settings(data_dir=Path(folder), token="visual-test-token", testing=True), DemoPolicy())
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver") as client:
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True, executable_path=os.getenv("BROWSER_EXECUTABLE") or shutil.which("chromium"))
                    page = await browser.new_page(viewport={"width": 1510, "height": 1100})
                    page.set_default_timeout(5000)
                    errors = []
                    print("visual: browser ready", flush=True)
                    page.on("pageerror", lambda exc: errors.append(str(exc)))
                    async def bridge(request):
                        response = await client.request(request.get("method", "GET"), request["url"],
                            headers=request.get("headers", {}), content=request.get("body"))
                        return {"status": response.status_code, "type": response.headers.get("content-type", ""),
                                "body": base64.b64encode(response.content).decode()}
                    await page.expose_function("__testBackend", bridge)
                    await page.evaluate("""() => {
                        window.fetch=async (url,options={})=>{const r=await window.__testBackend({url,...options});
                          const bytes=Uint8Array.from(atob(r.body),c=>c.charCodeAt(0));
                          return new Response(bytes,{status:r.status,headers:{'content-type':r.type}});};
                        window.WebSocket=class {constructor(){setTimeout(()=>this.onopen?.({}),0);}
                          send(){setTimeout(()=>this.onmessage?.({data:JSON.stringify({type:'STREAM_READY'})}),0);} close(){} };
                    }""")
                    html = (static / "index.html").read_text()
                    html = html.replace('<link rel="stylesheet" href="/static/console.css">', '<style>'+(static / "console.css").read_text()+'</style>')
                    html = html.replace('<script src="/static/console.js"></script>', '<script>'+(static / "console.js").read_text()+'</script>')
                    await page.set_content(html)
                    print("visual: content loaded", errors, flush=True)
                    await page.locator("#token").fill("visual-test-token")
                    await page.locator("#connect").click()
                    print("visual: connect clicked", errors, flush=True)
                    await page.wait_for_function("document.getElementById('profile').options.length>=2")
                    await page.locator("#profile").select_option("form-demo")
                    await page.locator("#mode").select_option("auto")
                    await page.locator("#autoClick").check()
                    await page.locator("#create").click()
                    print("visual: create clicked", errors, flush=True)
                    await page.wait_for_function("document.getElementById('owner').textContent==='HUMAN'")
                    await page.locator("#resume").click()
                    try:
                        await page.wait_for_function("document.getElementById('runstate').textContent==='COMPLETED'", timeout=10000)
                    except Exception:
                        for session in app.state.service.sessions.values():
                            print("visual failure session", json.dumps(session.public()), flush=True)
                            print("visual failure events", json.dumps((await app.state.service.store.events(session.id))[-10:]), flush=True)
                        print("visual page", await page.locator("#message").inner_text(), errors, flush=True)
                        raise
                    await page.evaluate("catchup()")
                    await page.wait_for_function("document.getElementById('screen').naturalWidth>0")
                    await page.screenshot(path=str(output / "console.png"), full_page=True)
                    sessions = list(app.state.service.sessions.values())
                    s = sessions[0]
                    events = await app.state.service.store.events(s.id)
                    report = {"provider": "demo-NOT-LAYA", "simulation": True,
                        "transport": "in-process ASGI bridge; not browser HTTP/WebSocket network validation",
                        "renderer": "real Chromium", "runtime": "real second Chromium instance with bundled local HTML",
                        "session_state": s.state.value, "steps": s.steps, "js_errors": errors,
                        "form_saved": await s.runtime.page.locator("#result").inner_text()=="Saved",
                        "executions": [e["payload"] for e in events if e["type"]=="ACTION_RESULT"],
                        "screenshot": "console.png"}
                    (output / "visual-smoke.json").write_text(json.dumps(report, indent=2))
                    print(json.dumps(report, indent=2))
                    assert s.state.value == "completed" and s.steps == 3 and not errors
                    await browser.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(".data/visual-smoke"))
    asyncio.run(run(parser.parse_args().output))
