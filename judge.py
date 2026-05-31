#!/usr/bin/env python3
"""
judge.py — Module B: the VIDEO vision-LLM-as-judge.

Replaces `mock_judge` in loop.py with a real Qwen-VL VIDEO judge running on
Modal. The whole point is a STABLE, parseable signal: incoherent / garbled /
artifacted clips must score ~0 so they can never win the loop.

    python judge.py                  # FREE mock fallback (no API / no Modal / no ffmpeg)
    USE_MOCK_JUDGE=0 python judge.py # real Qwen-VL video judge via Modal (needs secret)
    modal run judge.py               # real Qwen-VL video judge via Modal (always real)

Contract (loop.py calls this — see contracts.py):
    judge(sets: list[dict]) -> dict[str, dict]
        in : each set has  id, elements, video_url (https OR repo-relative .mp4);
             images/score/coherent ignored by the real path
        out: { set_id: {"score": float 0-10, "coherent": bool} }, one per input set

`JUDGE` is exported as an UPPERCASE alias of `judge` so BOTH of these work:
    from judge import judge as JUDGE   # what loop.py actually writes
    from judge import JUDGE            # the spec's name

VIDEO SEAM (the #1 risk — SETUP_FINDINGS SETUP 3):
    set['video_url'] is EITHER an absolute https URL (real Seedance result_url)
    OR a repo-root-relative path (mock / replay clip). Qwen-VL does NOT accept a
    raw mp4 URL, so the ADAPTER: download/resolve mp4 -> ffmpeg-sample K=3 evenly
    spaced frames -> base64 PNG data-URIs -> send as multiple image_url parts to
    IonRouter qwen3-vl-30b-a3b with response_format=json_object.

Scoring (per set, judged POINTWISE / independently to avoid ranking bias):
    coherence 0-10 = visual-style consistency ACROSS frames + a coherent micro-
                     story + frames well-formed (garbled / artifacted => near 0)
    creativity 0-10 = novelty / surprise / on-trend appeal
    GATE: score = creativity * (coherence / 10);  coherent = (coherence >= 5)
N_CALLS Qwen-VL calls per set are averaged (mean of coherence & creativity)
BEFORE the gate; N_CALLS=1 by default so one real verdict fits the per-set
container/JUDGE budget (low temp already makes a 2nd identical call redundant).
JSON is forced and parsed defensively. PER-SET FALLBACK:
if the adapter fails (download / ffmpeg / no frames), or Qwen rejects the format,
or any parse/exception occurs -> that set falls back to the MOCK judge (overlap
vs MOCK_TARGET) and logs loudly. Always one entry per set; NEVER raises.
"""
from __future__ import annotations

import os
import json
import base64

# Shared definitions — imported, never redefined (see contracts.py).
from contracts import MOCK_TARGET, K_ELEMENTS


# ---------------------------------------------------------------------------
# Project-level Modal auth — load credentials from a gitignored .modal.env so
# `python loop.py` / `python judge.py` authenticate to Modal on their own,
# with no global profile required. Runs before `import modal`; the Modal client
# reads MODAL_TOKEN_ID / MODAL_TOKEN_SECRET at connect time. Real env vars
# already set in the shell always win (setdefault never overwrites).
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


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# Default to the FREE mock path so `python judge.py` runs with zero deps/keys.
# loop.py only imports this module when its own USE_MOCK_JUDGE=0, so when the
# real judge is actually wired in, this reads "0" -> real path. Same convention.
USE_MOCK_JUDGE: bool = os.environ.get("USE_MOCK_JUDGE", "1") != "0"

# IonRouter is OpenAI-compatible: base_url + key come from the ionrouter-secret.
MODEL = "qwen3-vl-30b-a3b"     # SETUP 3: CONFIRMED LIVE, accepts image_url + json
# N_CALLS=1 (was 2): two identical low-temp (0.2) calls add ~no stability but
# DOUBLE the serial API cost (2*API_TIMEOUT), which alone can exceed the whole
# container budget and blow past the loop's JUDGE_BUDGET_SECS=120 assumption.
# One call keeps the real verdict comfortably inside one container timeout.
N_CALLS = 1                    # calls per set (was 2-averaged); 1 = fits the budget
N_FRAMES = 3                   # K evenly-spaced frames sampled per clip
FRAME_W = 384                  # downscale width (keep aspect) — token / cost guard
COHERENT_MIN = 5.0             # coherence gate threshold
DL_TIMEOUT = 20                # seconds — short timeout for the mp4 download
# API_TIMEOUT lowered 60->40 so worst-case serial wall time
# (DL + ffprobe + ffmpeg frames + N_CALLS*API_TIMEOUT) fits the container timeout
# and stays within the loop's JUDGE_BUDGET_SECS=120 demo assumption.
API_TIMEOUT = 40               # seconds — per chat/completions call

_SYSTEM = (
    "You are a strict visual judge for short-form social VIDEO content "
    "(TikTok-style dance clips). You are shown several frames sampled from one "
    "video. Judge ONLY the video frames, not prompt length or wordiness. "
    "Always respond with strict JSON."
)

_RUBRIC = (
    "These images are frames sampled in order from a SINGLE short video. "
    "Rate the video on two integer axes, each 0-10:\n"
    '- "coherence": visual-style consistency ACROSS the frames, a coherent '
    "micro-story from frame to frame, and whether the frames are well-formed. "
    "Garbled, noisy, scrambled, melting, or heavily artifacted frames MUST "
    "score near 0.\n"
    '- "creativity": novelty, surprise, and on-trend social appeal.\n'
    "Judge ONLY what you see in the frames. "
    'Respond with strict JSON exactly like: {"coherence": <0-10>, "creativity": <0-10>}'
)


def _clamp(x: float) -> float:
    """Clamp a numeric score into [0, 10]."""
    return max(0.0, min(10.0, float(x)))


def _repo_root() -> str:
    """Directory of this file == repo root (where demo_cache/, state.json live)."""
    return os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Mock scoring helper — shared by the FREE mock path AND the per-set real-path
# fallback so a degraded set still gets the SAME overlap signal the loop expects.
# ---------------------------------------------------------------------------
def _mock_score_one(elements: list) -> dict:
    """Overlap-vs-MOCK_TARGET verdict for ONE set (identical math to loop.py)."""
    overlap = len(set(elements or []) & MOCK_TARGET)
    score = 2.0 + 8.0 * (overlap / K_ELEMENTS)
    return {"score": float(score), "coherent": bool(score > 3.0)}


# ===========================================================================
# Modal app + the per-set VIDEO function
# ===========================================================================
# Guard the modal import so the FREE mock fallback works even where modal is
# not installed ("skip OpenAI/Modal"). When USE_MOCK_JUDGE=0 we genuinely need
# it and it will be present (loop.py / modal run environments have modal).
try:
    import modal
    _HAS_MODAL = True
except ImportError:  # pragma: no cover - only hit on a bare mock-only machine
    _HAS_MODAL = False


# ---- SEAM ADAPTER: video_url -> base64 PNG frame data-URIs ----------------
def _resolve_clip(video_url: str) -> tuple[str | None, bool]:
    """Resolve a set's video_url to a LOCAL mp4 path.

    Returns (local_path, is_temp). If video_url is an absolute http(s) URL we
    download it to a temp file (is_temp=True); if it's a repo-root-relative path
    we resolve it against the repo root (is_temp=False). Returns (None, False)
    on any failure — caller falls back to the mock judge for that set.
    """
    import tempfile

    url = (video_url or "").strip()
    if not url:
        print("[judge] empty video_url -> no clip")
        return None, False

    if url.startswith("http://") or url.startswith("https://"):
        try:
            import requests
            with requests.get(url, stream=True, timeout=DL_TIMEOUT) as r:
                r.raise_for_status()
                fd, tmp = tempfile.mkstemp(prefix="judge_clip_", suffix=".mp4")
                with os.fdopen(fd, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            f.write(chunk)
            if os.path.getsize(tmp) <= 0:
                print(f"[judge] downloaded clip is empty: {url[:80]}")
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                return None, False
            return tmp, True
        except Exception as e:
            print(f"[judge] clip download FAILED ({url[:80]}): {e!r}")
            return None, False

    # repo-root-relative local path (mock / replay clip)
    local = url if os.path.isabs(url) else os.path.join(_repo_root(), url)
    if os.path.isfile(local) and os.path.getsize(local) > 0:
        return local, False
    print(f"[judge] local clip not found / empty: {local}")
    return None, False


def _probe_duration(path: str) -> float:
    """Best-effort clip duration in seconds via ffprobe; 0.0 if unknown."""
    import subprocess
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=12,
        )
        return float((out.stdout or "").strip())
    except Exception as e:
        print(f"[judge] ffprobe duration failed for {path!r}: {e!r}")
        return 0.0


def _sample_frames(path: str, k: int = N_FRAMES) -> list[str]:
    """ffmpeg-sample k evenly-spaced frames -> list of base64 PNG data-URIs.

    Seeks to (2i+1)/(2k) of the clip duration for each frame (avoids black
    first/last frames, gives an even spread). Falls back to a frame-stride
    select filter if duration is unknown. Returns [] on any failure so the
    caller degrades that set to the mock judge.
    """
    import subprocess
    import tempfile

    dur = _probe_duration(path)
    uris: list[str] = []
    tmpdir = tempfile.mkdtemp(prefix="judge_frames_")
    try:
        if dur and dur > 0:
            for i in range(k):
                ts = round(dur * (2 * i + 1) / (2 * k), 3)
                out_png = os.path.join(tmpdir, f"f_{i}.jpg")
                try:
                    subprocess.run(
                        ["ffmpeg", "-y", "-ss", str(ts), "-i", path,
                         "-frames:v", "1", "-vf", f"scale={FRAME_W}:-2",
                         "-q:v", "5", out_png],   # JPEG: photographic frames stay tiny (PNG => 413)
                        capture_output=True, timeout=15,
                    )
                except Exception as e:
                    print(f"[judge] ffmpeg frame {i} (ts={ts}) failed: {e!r}")
                    continue
                if os.path.isfile(out_png) and os.path.getsize(out_png) > 0:
                    with open(out_png, "rb") as fh:
                        b64 = base64.b64encode(fh.read()).decode("ascii")
                    uris.append("data:image/jpeg;base64," + b64)
        else:
            # Unknown duration: grab the first k frames at a coarse stride.
            patt = os.path.join(tmpdir, "g_%02d.jpg")
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", path,
                     "-vf", f"select='not(mod(n\\,15))',scale={FRAME_W}:-2",
                     "-frames:v", str(k), "-vsync", "0", "-q:v", "5", patt],
                    capture_output=True, timeout=45,
                )
            except Exception as e:
                print(f"[judge] ffmpeg stride sampling failed: {e!r}")
            for i in range(1, k + 1):
                out_png = os.path.join(tmpdir, f"g_{i:02d}.jpg")
                if os.path.isfile(out_png) and os.path.getsize(out_png) > 0:
                    with open(out_png, "rb") as fh:
                        b64 = base64.b64encode(fh.read()).decode("ascii")
                    uris.append("data:image/jpeg;base64," + b64)
    finally:
        # Best-effort cleanup of the temp frame dir.
        try:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

    if not uris:
        print(f"[judge] no frames extracted from {path!r}")
    return uris


def _frames_to_uris(video_url: str) -> list[str]:
    """Full adapter: video_url -> [data:image/png;base64,...] (>=1) or []."""
    local, is_temp = _resolve_clip(video_url)
    if not local:
        return []
    try:
        return _sample_frames(local, N_FRAMES)
    finally:
        if is_temp:
            try:
                os.unlink(local)
            except OSError:
                pass


def _score_frames_real(frame_uris: list[str]) -> dict | None:
    """N_CALLS averaged Qwen-VL calls over the sampled FRAMES; apply the gate.

    Runs INSIDE the Modal container (openai + requests + ffmpeg present). Parses
    every response defensively: a failed / garbage call is logged and dropped.
    Returns None if EVERY call fails (caller then falls back to the mock judge
    for that set) — never raises.
    """
    from openai import OpenAI  # lazy: only available inside the Modal image

    # IonRouter is OpenAI-compatible. Key + base come from the ionrouter-secret.
    base_url = os.environ.get("IONROUTER_BASE_URL", "https://api.ionrouter.io/v1")
    api_key = os.environ.get("IONROUTER_KEY") or os.environ.get("IONROUTER_API_KEY")
    client = OpenAI(base_url=base_url, api_key=api_key, timeout=API_TIMEOUT)

    content = [{"type": "text", "text": _RUBRIC}]
    for uri in frame_uris:
        content.append({"type": "image_url", "image_url": {"url": uri}})
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": content},
    ]

    cohs: list[float] = []
    cres: list[float] = []
    for i in range(N_CALLS):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.2,          # low temp + averaging => stable signal
                max_tokens=80,
            )
            data = json.loads(resp.choices[0].message.content)
            cohs.append(_clamp(data["coherence"]))
            cres.append(_clamp(data["creativity"]))
        except Exception as e:  # parse error, missing key, API error, etc.
            print(f"[judge] call {i + 1}/{N_CALLS} parse/call failure: {e!r}")
            continue

    if not cohs:  # every call failed -> signal caller to use the per-set fallback
        return None

    coherence = sum(cohs) / len(cohs)
    creativity = sum(cres) / len(cres)
    score = creativity * (coherence / 10.0)   # GATE: incoherent => score ~0
    coherent = bool(coherence >= COHERENT_MIN)

    # --- Raindrop (additive, never-raise): record the RAW axes + gated result
    # for one set. Only flows on the local path; harmless/no-op on the Modal
    # container path (obs self-gates on USE_RAINDROP and swallows all errors).
    try:
        import obs
        if obs.USE_RAINDROP:
            obs.span("judge-set", {
                "raw_coherence": round(float(coherence), 3),
                "raw_creativity": round(float(creativity), 3),
                "gated_score": round(float(score), 3),
                "coherent": coherent,
            })
    except Exception:
        pass

    return {"score": round(float(score), 3), "coherent": coherent}


if _HAS_MODAL:
    app = modal.App("content-judge")

    image = (
        modal.Image.debian_slim()
        .pip_install("openai>=1.0", "requests")
        .apt_install("ffmpeg")                  # SEAM ADAPTER needs ffmpeg/ffprobe
        .add_local_python_source("contracts")   # container needs contracts.py
    )

    @app.function(
        image=image,
        secrets=[modal.Secret.from_name("ionrouter-secret")],
        max_containers=10,
        # timeout 120->150: give headroom above worst-case serial wall time
        # (DL_TIMEOUT 20 + ffprobe 12 + 3*ffmpeg 15=45 + N_CALLS=1 * API_TIMEOUT
        # 40 ~= 117s) so a real verdict is NOT clipped by the container timeout.
        # The realistic verdict still lands inside the loop's JUDGE_BUDGET_SECS.
        timeout=150,
        retries=modal.Retries(max_retries=2),
    )
    @modal.concurrent(max_inputs=10, target_inputs=8)
    def judge_one(set_dict: dict) -> dict:
        """Judge ONE video set: -> {"score": float 0-10, "coherent": bool}.

        Never raises. Adapter / Qwen failures degrade THIS set to the mock judge
        (overlap vs MOCK_TARGET), logged loudly, so a single bad set can never
        crash the fan-out and the loop still gets a usable signal.
        """
        sid = set_dict.get("id")
        elements = set_dict.get("elements") or []
        try:
            frame_uris = _frames_to_uris(set_dict.get("video_url", ""))
            if not frame_uris:
                print(f"[judge] set {sid!r}: adapter produced no frames "
                      f"-> mock fallback")
                return _mock_score_one(elements)
            verdict = _score_frames_real(frame_uris)
            if verdict is None:
                print(f"[judge] set {sid!r}: Qwen-VL gave no usable scores "
                      f"-> mock fallback")
                return _mock_score_one(elements)
            return verdict
        except Exception as e:  # pragma: no cover - last-resort guard
            print(f"[judge] judge_one hard failure for {sid!r}: {e!r} "
                  f"-> mock fallback")
            return _mock_score_one(elements)


# ===========================================================================
# FREE mock fallback — identical math to loop.py's mock_judge so the loop
# converges the same way when mocked.
# ===========================================================================
def _mock_judge(sets: list[dict]) -> dict[str, dict]:
    """Score by overlap with MOCK_TARGET: score = 2 + 8*overlap/K_ELEMENTS.

    Defensive on id: a set missing an 'id' key gets a synthetic one ("s{i}")
    rather than raising KeyError, so this NEVER raises on malformed input.
    """
    return {
        (s.get("id") or f"s{i}"): _mock_score_one(s.get("elements", []))
        for i, s in enumerate(sets)
    }


# ===========================================================================
# The contract entry point loop.py calls.
# ===========================================================================
def judge(sets: list[dict]) -> dict[str, dict]:
    """Score every set, returning {set_id: {"score", "coherent"}} (one per input).

    Real path fans out judge_one across Modal containers with
    return_exceptions=True; any per-set failure becomes a mock-judged verdict so
    the loop always gets a complete, well-formed result dict. NEVER raises.
    """
    if not sets:
        return {}

    if USE_MOCK_JUDGE:
        try:
            return _mock_judge(sets)
        except Exception as e:  # contract: judge() NEVER raises, even on bad input
            print(f"[judge] mock judge failed ({e!r}); degrading per-set")
            return {
                (s.get("id") or f"s{i}"): _mock_score_one(s.get("elements", []))
                for i, s in enumerate(sets)
            }

    if not _HAS_MODAL:  # asked for real but modal missing: degrade, don't crash
        print("[judge] USE_MOCK_JUDGE=0 but modal is unavailable; "
              "using mock overlap signal")
        return _mock_judge(sets)

    # Send only what the VIDEO judge needs: id (map back), video_url (the clip),
    # elements (for the per-set mock fallback).
    payloads = [
        {"id": s["id"], "video_url": s.get("video_url", ""),
         "elements": s.get("elements", [])}
        for s in sets
    ]

    results: dict[str, dict] = {}
    try:
        # app.run() lets a plain `python` process (loop.py) drive Modal; .map()
        # fans the per-set calls out across containers.
        with app.run():
            outs = list(judge_one.map(payloads, return_exceptions=True))
    except Exception as e:  # dispatch / connectivity failure: degrade everything
        print(f"[judge] Modal dispatch FAILED ({e!r}); using mock overlap signal")
        return _mock_judge(sets)

    for s, out in zip(sets, outs):
        if isinstance(out, BaseException) or not isinstance(out, dict):
            if isinstance(out, BaseException):
                print(f"[judge] set {s['id']!r} failed in map: {out!r} "
                      f"-> mock fallback")
            results[s["id"]] = _mock_score_one(s.get("elements", []))
        else:
            results[s["id"]] = {
                "score": float(out.get("score", 0.0)),
                "coherent": bool(out.get("coherent", False)),
            }
    return results


# Uppercase alias required by the spec; loop.py writes `import judge as JUDGE`.
JUDGE = judge


# ===========================================================================
# Demo harness — uses the real reference clips in demo_cache/clips/ so the
# VIDEO adapter (download/resolve -> ffmpeg frames -> base64) is exercised for
# real, plus one DELIBERATELY off-target/gibberish set so the lowest scorer is
# unambiguous in the mock path.
# ===========================================================================
def _demo_sets() -> list[dict]:
    """3 sample VIDEO sets incl. one off-target gibberish set (s2).

    video_url uses the repo-root-relative convention (mock/replay clip). The
    mock judge ignores video_url and scores on `elements` overlap; the real
    path samples frames from these clips.
    """
    return [
        {  # on-target elements, real reference clip
            "id": "s0",
            "elements": ["comedy", "dance-challenge", "90s"],
            "video_url": "demo_cache/clips/ref_tt-biz-01.mp4",
        },
        {  # partial-target, real reference clip
            "id": "s1",
            "elements": ["comedy", "neon", "slow-mo"],
            "video_url": "demo_cache/clips/ref_tt-biz-02.mp4",
        },
        {  # GIBBERISH: off-target elements + (deliberately) missing clip
            "id": "s2",
            "elements": ["zx-gibber", "glitch99", "qq-noise"],
            "video_url": "demo_cache/clips/__does_not_exist__.mp4",
        },
    ]


def _print_report(sets: list[dict], verdicts: dict[str, dict], mode: str) -> None:
    print(f"\n=== content-judge demo  (mode: {mode}) ===")
    print(f"{'set':<5}{'elements':<34}{'score':>7}  coherent")
    print("-" * 56)
    for s in sets:
        v = verdicts[s["id"]]
        els = ",".join(s["elements"])
        print(f"{s['id']:<5}{els[:32]:<34}{v['score']:>7.2f}  {v['coherent']}")
    lo = min(sets, key=lambda s: verdicts[s["id"]]["score"])
    lv = verdicts[lo["id"]]
    print("-" * 56)
    print(f"lowest scorer: {lo['id']}  score={lv['score']:.2f}  coherent={lv['coherent']}")
    print("  ^ the gibberish set (s2) must land here so it can never win the loop.")


if _HAS_MODAL:
    @app.local_entrypoint()
    def main() -> None:
        """`modal run judge.py` -> always real Qwen-VL video judge (app running)."""
        sets = _demo_sets()
        payloads = [
            {"id": s["id"], "video_url": s["video_url"], "elements": s["elements"]}
            for s in sets
        ]
        outs = list(judge_one.map(payloads, return_exceptions=True))
        verdicts = {}
        for s, o in zip(sets, outs):
            if isinstance(o, dict):
                verdicts[s["id"]] = o
            else:
                print(f"[judge] set {s['id']!r} failed in map: {o!r} -> mock fallback")
                verdicts[s["id"]] = _mock_score_one(s.get("elements", []))
        _print_report(sets, verdicts, mode="modal Qwen-VL video judge")


if __name__ == "__main__":
    _sets = _demo_sets()
    _mode = ("FREE mock (overlap vs MOCK_TARGET)" if USE_MOCK_JUDGE
             else "real Qwen-VL video judge via Modal")
    print(f"content-judge | USE_MOCK_JUDGE={'1' if USE_MOCK_JUDGE else '0'} -> {_mode}")
    _verdicts = judge(_sets)
    _print_report(_sets, _verdicts, mode=_mode)
