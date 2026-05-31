# The Cringe Filter 🪤

A **video-native self-improving TikTok loop**. It generates short vertical clips, judges its own
output with a vision LLM, and learns what works — incoherent/garbled clips get filtered to ~0 so
junk can never win.

- **Generate** — Seedance 2.0 video via TokenRouter (`/video/generations`, imarouter fallback), on Modal.
- **Judge** — Qwen-VL (`qwen3-vl-30b-a3b`) over ffmpeg-sampled frames via IonRouter; `score = creativity × (coherence/10)`, `coherent = coherence ≥ 5`.
- **Learn** — Hedge (multiplicative-weights) policy over 12 creative elements; converges toward winning style combos.
- **Trends** — `trending.py` pulls live YouTube `chart=mostPopular` (falls back to a cached list, never blocks).
- **Observe** — Raindrop Workshop traces + a bulletproof `state.json` alarm mirror (plateau / mode-collapse / judge-degradation) surfaced on the dashboard.
- **Dashboard** — dark "Policy Trainer" UI: policy bars, score-climbing line, live `<video>` candidate grid, Loop Activity feed + alarm banner.

## Run it

```bash
# Free local demo (no API/cloud) — converges, plays cached real clips
USE_MOCK=1 USE_MOCK_JUDGE=1 python loop.py
python serve_dashboard.py            # http://localhost:8000

# Replay the cached real run (zero API calls)
DEMO_REPLAY=1 python serve_dashboard.py

# Real pass (Seedance clips + Qwen-VL judging)
USE_MOCK=0 USE_MOCK_JUDGE=0 python loop.py     # via Modal
python real_pass.py                            # local-orchestrated equivalent
```

Toggles: `USE_MOCK` (generation), `USE_MOCK_JUDGE` (judging), `USE_RAINDROP` (observability, default on),
`DEMO_DEADLINE_SECS` (wall-clock cap), `MAX_VIDEOS` (credit cap), `DEMO_REPLAY`.

## Layout

| File | Role |
|---|---|
| `contracts.py` | shared shapes (`ContentSet`, `State`, `Raindrop`), config, vocab |
| `generate.py` | Seedance video generator (Modal) |
| `judge.py` | Qwen-VL frame-sampling judge (Modal) |
| `loop.py` | Hedge control loop + guards + Raindrop instrumentation |
| `trending.py` | YouTube trending source (cached fallback) |
| `obs.py` | Raindrop Workshop SDK + bulletproof `state.json` mirror |
| `dashboard.html` / `serve_dashboard.py` | live dashboard + static server |
| `real_pass.py` | local real-pass runner |

> Secrets live in a gitignored `.modal.env` (Modal token, OpenAI/router/YouTube keys) — not committed.
