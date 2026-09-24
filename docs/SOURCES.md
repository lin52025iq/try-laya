# Verified upstream contract

Checked on 2026-09-24. These references establish interface shape, not this project's measured performance.

- Laya release 0.3.18: https://pypi.org/project/laya/0.3.18/
- Upstream project: https://github.com/NandhaKishorM/laya
- Router implementation: https://github.com/NandhaKishorM/laya/blob/main/laya/router.py
  - Retrieved blob SHA: `6becc64680c6a56a506b57ec3d33666bd208b312`.
  - `predict(state, questions, model=..., max_len=..., head_max_len=...)`, preloading and unload.
- Server implementation: https://github.com/NandhaKishorM/laya/blob/main/laya/serve.py
  - Retrieved blob SHA: `c0fc1bc3b8024c92db8103c4436aca2fceabe677`.
  - `GET /health`, `POST /v1/systemone` with state/questions/model; bearer auth when configured.
- Decoder implementation: https://github.com/NandhaKishorM/laya/blob/main/laya/agent.py
  - Retrieved blob SHA: `b0fb78a75a7dc51e971b15409840c44871455f31`.
  - `answers[id].choice/probabilities/answer_confidence`; legacy choice `confidence` is entropy-based, not selected-answer probability.

The adapter uses the chosen candidate's probability and probability margin. Those numbers are not validated gameplay success probabilities. No upstream timing or benchmark result is reported as a local result. Laya is an optional dependency pinned to the inspected release; the project does not vendor model weights or upstream implementation code.
