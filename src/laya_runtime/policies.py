"""Real Laya SDK/HTTP adapters and an explicitly labelled, non-AI demo policy."""
from __future__ import annotations

import asyncio
import functools
import importlib.util
import math
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

import httpx

from .models import Decision


class PolicyError(RuntimeError):
    pass


class Policy(Protocol):
    name: str
    async def predict(self, state: dict, questions: dict, budget_s: float) -> Decision: ...
    async def warmup(self) -> None: ...
    async def close(self) -> None: ...


def decode(response: dict, questions: dict, provider: str, latency_ms: float,
           max_input_tokens: int = 1024) -> Decision:
    """Use probabilities/answer_confidence, not Laya's entropy-based `confidence`."""
    try:
        answer = response["answers"]["next"]
        choice = answer["choice"]
        permitted = questions["next"]["criteria"]
        probs = answer["probabilities"]
        if choice not in permitted or set(probs) != set(permitted):
            raise ValueError("Returned choice/distribution does not match candidate set")
        values = [float(x) for x in probs.values()]
        if any(not math.isfinite(v) or v < 0 or v > 1 for v in values):
            raise ValueError("Invalid probabilities")
        if abs(sum(values) - 1) > 0.015:
            raise ValueError("Unnormalised probability distribution")
        ranked = sorted(values, reverse=True)
        p = float(probs[choice])
        if p < ranked[0] - 0.001:
            raise ValueError("Choice is inconsistent with distribution")
        # One question: a result at the context cap may have silently truncated state.
        if response.get("usage", {}).get("input_tokens", 0) >= max_input_tokens:
            raise ValueError("Possible context truncation; shorten profile/observation")
        return Decision(candidate_id=choice, provider=provider, probability=p,
                        margin=max(0, p - ranked[1]) if len(ranked) > 1 else p,
                        latency_ms=latency_ms,
                        routing_model=str(response.get("routing", {}).get("model", "")))
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyError(f"Invalid Laya response: {exc}") from exc


class LocalLaya:
    name = "laya-local"

    def __init__(self, model: str = "multilingual", device: str = "cpu", threads: int = 2):
        self.model, self.device, self.threads = model, device, threads
        self._router = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="laya-inference")
        self._inflight: asyncio.Future | None = None
        self.ready = False

    def _load(self):
        try:
            import torch
            from laya import Router
        except ImportError as exc:
            raise PolicyError("Install try-laya[laya] and run agentctl warmup; no demo fallback is used") from exc
        torch.set_num_threads(self.threads)
        router = Router(device=self.device, max_loaded=1)
        router.preload([self.model])
        self._router = router
        self.ready = True

    async def warmup(self) -> None:
        if self.ready:
            return
        if self._inflight is not None and not self._inflight.done():
            raise PolicyError("Model worker is busy")
        self._inflight = asyncio.get_running_loop().run_in_executor(self._pool, self._load)
        await asyncio.shield(self._inflight)

    async def predict(self, state: dict, questions: dict, budget_s: float) -> Decision:
        if not self.ready:
            raise PolicyError("Laya is not warm. Warm up before starting an episode")
        # Crucial: cancellation of an asyncio future cannot stop a running torch forward.
        # Keep tracking it and REFUSE to enqueue another stale request behind it.
        if self._inflight is not None and not self._inflight.done():
            raise PolicyError("Laya worker busy with a previous inference; no request queued")
        start = time.monotonic()
        call = functools.partial(self._router.predict, state, questions, model=self.model,
                                 max_len=1024, head_max_len=512)
        self._inflight = asyncio.get_running_loop().run_in_executor(self._pool, call)
        try:
            response = await asyncio.wait_for(asyncio.shield(self._inflight), timeout=max(.001, budget_s))
        except asyncio.TimeoutError as exc:
            raise PolicyError("Laya decision deadline exceeded; late result cannot execute") from exc
        except Exception as exc:
            raise PolicyError(f"Laya inference failed: {type(exc).__name__}") from exc
        return decode(response, questions, self.name, (time.monotonic() - start) * 1000)

    async def close(self) -> None:
        await asyncio.to_thread(self._pool.shutdown, wait=True, cancel_futures=True)
        if self._router:
            self._router.unload()


class RemoteLaya:
    name = "laya-http"

    def __init__(self, base_url: str, model: str = "multilingual", api_key: str = "",
                 transport: httpx.AsyncBaseTransport | None = None):
        self.base_url, self.model = base_url.rstrip("/"), model
        self._client = httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
        self._gate = asyncio.Lock()

    async def warmup(self) -> None:
        response = await self._client.get(f"{self.base_url}/health", timeout=10)
        response.raise_for_status()
        # Server must preload its selected model, not merely answer /health.
        if self.model not in response.json().get("loaded", []):
            raise PolicyError(f"Remote server has not preloaded {self.model}")

    async def predict(self, state: dict, questions: dict, budget_s: float) -> Decision:
        if self._gate.locked():
            raise PolicyError("Remote inference busy; no stale queue")
        start = time.monotonic()
        async with self._gate:
            try:
                response = await self._client.post(f"{self.base_url}/v1/systemone",
                    json={"state": state, "questions": questions, "model": self.model},
                    timeout=max(.001, budget_s))
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise PolicyError(f"Laya HTTP failure: {type(exc).__name__}") from exc
        cap = 512 if self.model == "english" else 1024
        return decode(response.json(), questions, self.name, (time.monotonic() - start) * 1000, cap)

    async def close(self) -> None:
        await self._client.aclose()


class DemoPolicy:
    """No learned model. Only validates plumbing. NEVER report its latency as Laya latency."""
    name = "demo-NOT-LAYA"

    async def warmup(self) -> None:
        pass

    async def predict(self, state: dict, questions: dict, budget_s: float) -> Decision:
        choice = next(k for k in questions["next"]["criteria"] if not k.startswith("__"))
        # Explicit scripted demonstration of the synthetic sandbox; this is NOT learned skill.
        allowed = questions["next"]["criteria"]
        offset = state.get("untrusted_observation", {}).get("fields", {}).get("offset", {}).get("number")
        if {"right", "left", "neutral"} <= set(allowed) and isinstance(offset, (float, int)):
            choice = "right" if offset > 5 else "left" if offset < -5 else "neutral"
        return Decision(candidate_id=choice, provider=self.name, probability=1, margin=1, latency_ms=0)

    async def close(self) -> None:
        pass


def sdk_available() -> bool:
    return importlib.util.find_spec("laya") is not None
