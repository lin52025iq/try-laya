# Engineering invariants

- Laya is the default reactive policy. Never silently fall back to Demo or per-action LLM.
- Keep model inference outside the execution lock; a timed-out native worker still running must not accept queued stale requests.
- Models propose data-only candidates. Runtime side effects use the guarded Service executor only.
- Preserve human takeover, Goal/Policy/Control/Episode invalidation, target bindings, bounded TTL and neutral release.
- Keep transactional fingerprint checks separate from realtime observation-age checks.
- Profile imports and LLM drafts never activate or grant permissions automatically.
- Do not conflate dispatched input, verified effect, or completed goal.
- Test real browser DOM separately from fake model/transport tests. Skipped hardware/model tests are not passed tests.
- Never commit .data, tokens, user screenshots, browser state, downloaded weights, or generated machine-specific artifacts.
- Run `python -m pytest -q` and update docs/VALIDATION.md honestly. Do not describe a synthetic sandbox as FPS validation.
