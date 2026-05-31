#!/usr/bin/env python3
"""
generate.py — Module A: parallel Seedance VIDEO generator on Modal 1.x.

Takes N prompts, dispatches ALL of them in a SINGLE `generate_one.map(...)` call
so Modal lights up many containers at once. Each container submits one Seedance
clip, polls it to completion, and returns the presigned mp4 result_url. Results
are regrouped into one inner list per original prompt, in input order.

Contract (loop.py does `from generate import GENERATE`):
    GENERATE(prompts: list[str]) -> list[list[str]]
        one inner list PER prompt, EACH OF LENGTH 1, carrying the clip reference
        (a `video_url` string); output order matches input order. NEVER raises.

The clip reference (per the VIDEO_URL convention) is EITHER:
    * an absolute https URL  -> a real Seedance result_url (presigned, 24h), OR
    * a repo-root-relative path like `demo_cache/clips/foo.mp4` (mock / fallback).
Both play directly in the dashboard's <video src> (it serves the repo root).

Run modes:
    USE_MOCK=1 python generate.py # FREE local path: cached-clip placeholders, no API
    python generate.py            # same (auto-forced to mock for a bare `python` run)
    modal run generate.py         # real Seedance fan-out, prints timing + ok/fail

Real path needs a Modal secret "seedance-secret" exposing (per SETUP_FINDINGS):
    SEEDANCE_API_KEY, SEEDANCE_BASE_URL (=https://api.tokenrouter.com/v1),
    SEEDANCE_MODEL  (=dreamina-seedance-2-0-fast-260128),
    IMAROUTER_API_KEY, IMAROUTER_BASE_URL (=https://api.imarouter.com/v1),
    IMAROUTER_MODEL (=seedance-2.0-fast-cn)   # fallback router
Modal auth is loaded from the gitignored .modal.env (same convention as judge.py),
so `USE_MOCK=0 python loop.py` authenticates even when the judge is mocked.
"""

import os
import glob
import time

from contracts import DEMO_DEADLINE_SECS, MAX_VIDEOS


# ---------------------------------------------------------------------------
# Project-level Modal auth + local secret fallback — load credentials from a
# gitignored .modal.env so a plain `python` process (loop.py importing this when
# its judge is mocked) authenticates to Modal on its own. Must run BEFORE
# `import modal`. Real env vars already in the shell always win (setdefault).
# Mirrors judge.py:_load_project_env so both modules behave identically.
# ---------------------------------------------------------------------------
def _load_project_env(filename: str = ".modal.env") -> None:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())
    except FileNotFoundError:
        pass  # mock path needs no creds; real path can fall back to a profile


_load_project_env()


# Repo root — used to express cached clips as repo-root-relative paths.
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# Seedance clip params (per SETUP_FINDINGS SETUP 2). The fast model normalizes
# duration/resolution internally; ratio is honored, so we send 9:16 for TikTok.
SEEDANCE_RATIO = "9:16"
SEEDANCE_DURATION = 5
SEEDANCE_RESOLUTION = "480p"

# Per-clip poll budget — DECOUPLED from the whole-demo deadline (non-negotiable b).
# A single Seedance clip takes ~100-110s. If we waited the FULL DEMO_DEADLINE_SECS
# (240s) here, one generate() call could legitimately poll for the entire demo
# budget, and iter 1 (which always runs to completion) — generate + ~120s judge —
# would blow past the deadline before run_loop's pre-iteration guard could act.
# So bound the poll to a PER-CLIP budget (~150s: a clip + a little slack), strictly
# *inside* the deadline, so generate+judge for the first iteration still fits under
# DEMO_DEADLINE_SECS. A stuck job degrades to a placeholder instead of hanging.
_POLL_TIMEOUT = max(30, min(150, int(DEMO_DEADLINE_SECS) - 90))


# ---------------------------------------------------------------------------
# Optional modal import — the FREE local path must work even where modal is not
# installed. When USE_MOCK=0 we genuinely need it (loop.py / `modal run` have it).
# ---------------------------------------------------------------------------
try:
    import modal
    _HAS_MODAL = True
except ImportError:  # pragma: no cover - only on a bare mock-only machine
    _HAS_MODAL = False


# ===========================================================================
# Placeholders — never let a live demo break. Repo-root-relative cached clips
# so the dashboard can actually PLAY the fallback (per the VIDEO_URL convention).
# ===========================================================================
def _cached_clip_refs() -> list:
    """Repo-root-relative paths to every cached .mp4 under demo_cache/clips/.

    Returns [] if none exist. Sorted for deterministic cycling. A primary
    sample (sample_seedance.mp4) is floated to the front when present so the
    single-clip placeholder is a sensible, known-good file.
    """
    pattern = os.path.join(_REPO_ROOT, "demo_cache", "clips", "*.mp4")
    try:
        abs_paths = sorted(glob.glob(pattern))
    except Exception as exc:  # pragma: no cover - glob basically never raises
        print(f"[generate] WARN could not scan cached clips: {exc!r}")
        abs_paths = []
    rels = [os.path.relpath(p, _REPO_ROOT) for p in abs_paths]
    # Prefer the canonical sample as the lead fallback if it's there.
    rels.sort(key=lambda r: (os.path.basename(r) != "sample_seedance.mp4", r))
    return rels


# Resolved once at import; safe even when the dir is missing (-> []).
_CACHED_CLIPS = _cached_clip_refs()


def _placeholder_ref(i: int = 0) -> str:
    """One clip reference for failure/cap/mock: cycle cached clips, else ''."""
    if _CACHED_CLIPS:
        return _CACHED_CLIPS[i % len(_CACHED_CLIPS)]
    return ""  # nothing cached -> empty, safe ref; dashboard shows no video


def local_generate(prompts: list) -> list:
    """Zero-cost stand-in for GENERATE: cached-clip refs, ONE per prompt.

    Cycles through whatever .mp4 files live under demo_cache/clips/ so each set
    gets a (likely distinct) playable clip. Falls back to '' if none exist.
    """
    return [[_placeholder_ref(i)] for i, _ in enumerate(prompts)]


# ===========================================================================
# Modal app + the per-clip function (only defined when modal is present).
# ===========================================================================
if _HAS_MODAL:
    app = modal.App("content-gen-video")

    image = (
        modal.Image.debian_slim()
        .pip_install("requests")
        .add_local_python_source("contracts")   # container imports generate.py -> needs contracts.py
    )

    @app.function(
        image=image,
        secrets=[modal.Secret.from_name("seedance-secret")],
        max_containers=10,
        # One clip ~100-110s; allow generous submit+poll headroom but stay finite.
        timeout=max(180, _POLL_TIMEOUT + 60),
        retries=modal.Retries(max_retries=2, backoff_coefficient=2.0, initial_delay=3.0),
    )
    @modal.concurrent(max_inputs=12, target_inputs=10)
    async def generate_one(prompt: str) -> str:
        """Submit ONE Seedance clip, poll to completion, return its result_url.

        Async + @modal.concurrent => many clips submitted+polled CONCURRENTLY
        within a few containers (submit-all-then-poll-all emerges naturally:
        every input is its own coroutine, all in flight at once). On ANY failure
        this returns '' (never raises); .map regroups '' into a placeholder ref.
        """
        import asyncio
        import requests

        def _cfg():
            """Read router config, tolerating both SEEDANCE_* and TOKENROUTER_* names."""
            primary = {
                "name": "tokenrouter",
                "key": os.environ.get("SEEDANCE_API_KEY") or os.environ.get("TOKENROUTER_API_KEY", ""),
                "base": (os.environ.get("SEEDANCE_BASE_URL")
                         or os.environ.get("TOKENROUTER_BASE_URL")
                         or "https://api.tokenrouter.com/v1").rstrip("/"),
                "model": os.environ.get("SEEDANCE_MODEL", "dreamina-seedance-2-0-fast-260128"),
            }
            fallback = {
                "name": "imarouter",
                "key": os.environ.get("IMAROUTER_API_KEY", ""),
                "base": (os.environ.get("IMAROUTER_BASE_URL") or "https://api.imarouter.com/v1").rstrip("/"),
                "model": os.environ.get("IMAROUTER_MODEL", "seedance-2.0-fast-cn"),
            }
            return primary, fallback

        def _headers(key: str) -> dict:
            return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

        def _submit(router: dict) -> str:
            """POST /video/generations -> task_id (blocking; run in a thread)."""
            payload = {
                "model": router["model"],
                "prompt": prompt,                       # plain text, NO --flags
                "ratio": SEEDANCE_RATIO,
                "duration": SEEDANCE_DURATION,
                "resolution": SEEDANCE_RESOLUTION,
            }
            r = requests.post(
                f"{router['base']}/video/generations",
                headers=_headers(router["key"]), json=payload, timeout=60,
            )
            r.raise_for_status()
            body = r.json()
            tid = body.get("task_id") or body.get("id") or body.get("request_id")
            if not tid:
                raise RuntimeError(f"no task_id in submit response: {str(body)[:160]}")
            return tid

        def _poll_once(router: dict, task_id: str):
            """GET /video/generations/{id} -> (terminal_status|None, result_url|None)."""
            r = requests.get(
                f"{router['base']}/video/generations/{task_id}",
                headers=_headers(router["key"]), timeout=60,
            )
            r.raise_for_status()
            body = r.json()
            data = body.get("data", body) if isinstance(body, dict) else {}
            if not isinstance(data, dict):
                data = {}
            status = str(data.get("status", "")).upper()
            result_url = data.get("result_url")
            if not result_url:
                # nested shape: data.data.content.video_url
                inner = data.get("data") or {}
                if isinstance(inner, dict):
                    content = inner.get("content") or {}
                    if isinstance(content, dict):
                        result_url = content.get("video_url")
            if status in {"SUCCESS"}:
                return "SUCCESS", result_url
            if status in {"FAILED", "CANCELLED", "CANCELED", "ERROR"}:
                return status, None
            return None, None  # still IN_PROGRESS / QUEUED

        async def _run_router(router: dict) -> str:
            if not router["key"]:
                raise RuntimeError(f"{router['name']} has no API key configured")
            task_id = await asyncio.to_thread(_submit, router)
            deadline = time.monotonic() + _POLL_TIMEOUT
            await asyncio.sleep(8)  # nothing is ready before ~100s; cheap first wait
            while True:
                status, url = await asyncio.to_thread(_poll_once, router, task_id)
                if status == "SUCCESS" and url:
                    return url
                if status in {"FAILED", "CANCELLED", "CANCELED", "ERROR"}:
                    raise RuntimeError(f"{router['name']} task {task_id} -> {status}")
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{router['name']} task {task_id} timed out after {_POLL_TIMEOUT}s")
                await asyncio.sleep(5)

        primary, fallback = _cfg()
        # Primary (tokenrouter); on ANY failure, try the imarouter fallback.
        try:
            return await _run_router(primary)
        except Exception as exc:
            print(f"[generate_one] primary {primary['name']} failed: {exc!r} — trying fallback")
        try:
            return await _run_router(fallback)
        except Exception as exc:
            print(f"[generate_one] fallback {fallback['name']} failed: {exc!r} — placeholder")
            return ""  # regrouped to a cached-clip ref by _fanout; never raises


# ---------------------------------------------------------------------------
# Core fan-out + regroup (assumes a running Modal app context).
# ---------------------------------------------------------------------------
def _fanout(prompts: list):
    """Dispatch up to MAX_VIDEOS clips via ONE .map(), regroup in order.

    Beyond the MAX_VIDEOS cap, prompts are NOT generated — they get a cached-clip
    placeholder ref instead (credit guard). Returns (out, ok, fail, skipped).
    Each inner list has length 1 (the contract). Never raises here; per-clip
    errors arrive as exceptions/'' and degrade to a placeholder.
    """
    n = len(prompts)
    cap = max(0, int(MAX_VIDEOS))
    live_idx = list(range(min(n, cap)))          # which prompts actually generate
    live_prompts = [prompts[i] for i in live_idx]

    results: list = []
    if live_prompts:
        # ONE map call — every clip dispatched at once (the megastructure lights up)
        results = list(generate_one.map(live_prompts, return_exceptions=True))

    res_by_idx = dict(zip(live_idx, results))

    out, ok, fail, skipped = [], 0, 0, 0
    for i in range(n):
        if i not in res_by_idx:                  # over the MAX_VIDEOS cap
            out.append([_placeholder_ref(i)])
            skipped += 1
            continue
        r = res_by_idx[i]
        if isinstance(r, str) and r.startswith("http"):
            out.append([r])                      # real presigned Seedance URL
            ok += 1
        else:                                    # Exception, '' , or unexpected
            out.append([_placeholder_ref(i)])
            fail += 1
    return out, ok, fail, skipped


# ===========================================================================
# THE CONTRACT — importable, loop.py calls this directly. Never raises.
# Returns list[list[str]]: one inner list PER prompt, EACH OF LENGTH 1.
# ===========================================================================
def GENERATE(prompts: list) -> list:
    """prompts -> [[video_url], ...] in input order. Length-1 inner lists. Never raises."""
    prompts = list(prompts or [])
    if not prompts:
        return []

    if os.environ.get("USE_MOCK") == "1":
        t0 = time.time()
        out = local_generate(prompts)
        kind = "cached-clip" if _CACHED_CLIPS else "empty"
        print(f"[generate:mock] {len(prompts)} {kind} placeholders "
              f"for {len(prompts)} prompts in {time.time() - t0:.2f}s")
        return out

    if not _HAS_MODAL:  # asked for real but modal missing: degrade, don't crash
        print("[generate] USE_MOCK=0 but modal is unavailable; returning cached-clip placeholders")
        return [[_placeholder_ref(i)] for i in range(len(prompts))]

    t0 = time.time()
    try:
        with app.run():                          # ephemeral app for direct `python` use
            out, ok, fail, skipped = _fanout(prompts)
    except Exception as exc:
        # app.run()/secret/dispatch failed entirely — degrade to a full placeholder
        # grid so the live demo never dies. (Per-clip errors handled in _fanout.)
        out = [[_placeholder_ref(i)] for i in range(len(prompts))]
        ok, fail, skipped = 0, len(prompts), 0
        print(f"[generate] FATAL {exc!r} — returning {len(prompts)} cached-clip placeholders")
    print(f"[generate] {ok} ok / {fail} fail / {skipped} capped  |  {len(prompts)} prompts "
          f"(MAX_VIDEOS={MAX_VIDEOS})  in {time.time() - t0:.1f}s")
    return out


# lowercase alias — keeps contracts.py's documented name and any older import working
generate = GENERATE


# ---------------------------------------------------------------------------
# `modal run generate.py` — proves the parallel video fan-out end to end.
# ---------------------------------------------------------------------------
if _HAS_MODAL:
    @app.local_entrypoint()
    def main():
        prompts = [
            "vertical TikTok dance, neon 90s high-school comedy, energetic dancer",
            "vertical TikTok dance, dark emo rock-and-roll slow-mo, moody lighting",
            "vertical TikTok dance, wholesome retro-film dance-challenge, warm tones",
        ]
        t0 = time.time()
        out, ok, fail, skipped = _fanout(prompts)   # app already running under `modal run`
        dt = time.time() - t0

        n_clips = sum(len(x) for x in out)
        print("\n=== content-gen-video ===")
        print(f"{len(prompts)} prompts -> {n_clips} clips (1/prompt)  "
              f"ok={ok} fail={fail} capped={skipped}  wall={dt:.1f}s")
        assert len(out) == len(prompts), "regroup lost prompts"
        assert all(len(inner) == 1 for inner in out), "inner list must be length 1"
        assert all(isinstance(s, str) for inner in out for s in inner), "ref must be str"
        for i, inner in enumerate(out):
            ref = inner[0]
            print(f"  prompt {i}: {ref[:72]}{'…' if len(ref) > 72 else ''}")
        print("contract OK ✓")


if __name__ == "__main__":
    # Plain `python generate.py` only makes sense for the free local path.
    if os.environ.get("USE_MOCK") != "1":
        print("tip: run `modal run generate.py` for the real Seedance fan-out, "
              "or `USE_MOCK=1 python generate.py` for the free local test.")
        os.environ["USE_MOCK"] = "1"
    out = GENERATE([
        "neon 90s comedy dance", "dark emo slow-mo dance",
        "wholesome retro dance-challenge", "chaotic neon comedy dance",
    ])
    n_inner = len(out[0]) if out else 0
    first = out[0][0][:60] if out and out[0] and out[0][0] else "(empty ref)"
    print(f"local GENERATE -> {len(out)} sets x {n_inner} ref(s); first ref: {first}")
    # Contract self-check on the mock path.
    assert all(len(inner) == 1 for inner in out), "inner list must be length 1"
    assert all(isinstance(s, str) for inner in out for s in inner), "ref must be str"
    print("mock contract OK ✓")
