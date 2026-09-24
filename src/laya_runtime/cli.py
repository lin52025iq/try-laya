from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .api import Settings, create_app, make_policy
from .models import Observation, Profile
from .policies import sdk_available
from .profiles import build_request, builtin_profiles, candidates


async def benchmark(settings: Settings, iterations: int) -> dict:
    policy = make_policy(settings)
    try:
        await policy.warmup()
        pack = builtin_profiles()["form-demo"]
        state = {"fields": {"name": {"present": True, "count": 1, "empty": True},
                            "company": {"present": True, "count": 1, "empty": False},
                            "submit": {"present": True, "count": 1}, "result": {"text": ""}}}
        observation = Observation(runtime_id="benchmark-fixture", state=state, fingerprint="fixture")
        options = candidates(pack, observation, {"name": "Test", "company": "Example"})
        model_state, questions = build_request(pack, "Fill the required fields", observation, options)
        results = []
        for _ in range(iterations):
            started = time.monotonic()
            decision = await policy.predict(model_state, questions, 30)
            results.append({"choice": decision.candidate_id, "correct": decision.candidate_id == "fill_name",
                            "probability": decision.probability, "elapsed_ms": (time.monotonic()-started)*1000})
        times = sorted(r["elapsed_ms"] for r in results)
        return {"provider": policy.name, "simulation": policy.name.startswith("demo"),
                "scope": "one synthetic decision fixture; NOT browser/game end-to-end latency",
                "platform": platform.platform(), "python": sys.version.split()[0],
                "iterations": iterations, "correct": sum(r["correct"] for r in results),
                "p50_ms": times[len(times)//2], "p95_ms": times[min(len(times)-1, int(len(times)*.95))],
                "results": results}
    finally:
        await policy.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Laya-first Runtime. Demo mode is NOT model inference.")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--headed", action="store_true")
    serve.add_argument("--policy", choices=["local", "http", "demo"], default=None)
    sub.add_parser("doctor")
    sub.add_parser("browser-install")
    check = sub.add_parser("check-profile")
    check.add_argument("path", type=Path)
    warm = sub.add_parser("warmup")
    warm.add_argument("--policy", choices=["local", "http"], default=None)
    bench = sub.add_parser("benchmark")
    bench.add_argument("--policy", choices=["local", "http", "demo"], default=None)
    bench.add_argument("--iterations", type=int, default=10)
    bench.add_argument("--output", type=Path)
    args = parser.parse_args()
    settings = Settings()
    if getattr(args, "policy", None):
        settings.policy_backend = args.policy
    try:
        if args.command == "serve":
            import uvicorn
            settings.headless = not args.headed
            print(f"Console: http://127.0.0.1:{args.port}\nBearer token (paste in console): {settings.token}", flush=True)
            if settings.policy_backend == "demo":
                print("WARNING: deterministic DEMO policy, no Laya model is used.", flush=True)
            uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, log_level="info")
        elif args.command == "doctor":
            print(json.dumps({"python": sys.version.split()[0], "platform": platform.platform(),
                "chromium": settings.executable or "Playwright-managed (run browser-install)",
                "laya_sdk_installed": sdk_available(), "policy": settings.policy_backend,
                "model": settings.laya_model, "device": settings.laya_device,
                "adb": shutil.which("adb"), "data_directory": str(settings.data_dir.resolve()),
                "note": "Installed SDK does not prove downloaded weights or inference quality."}, indent=2))
        elif args.command == "browser-install":
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
        elif args.command == "check-profile":
            pack = Profile.model_validate_json(args.path.read_text(encoding="utf-8"))
            print(json.dumps({"valid": True, "id": pack.id, "hash": pack.content_hash,
                              "note": "Schema validity is not evidence of gameplay competence."}, indent=2))
        elif args.command == "warmup":
            async def run():
                policy = make_policy(settings)
                try:
                    await policy.warmup()
                    print(f"Warmed: {policy.name}; next run benchmark to check an actual prediction")
                finally:
                    await policy.close()
            asyncio.run(run())
        elif args.command == "benchmark":
            if not 1 <= args.iterations <= 1000:
                raise ValueError("iterations must be 1..1000")
            result = asyncio.run(benchmark(settings, args.iterations))
            text = json.dumps(result, indent=2)
            if args.output:
                args.output.write_text(text + "\n", encoding="utf-8")
            print(text)
    except (Exception, KeyboardInterrupt) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
