#!/usr/bin/env python3
"""
loop.py — self-improving content-generation orchestrator (hackathon build).

Module C in the pipeline. It ties together the GENERATOR (generate.py) and the
JUDGE (judge.py) and updates a `policy`: a weight dict over the 12 creative
elements. Each iteration we sample element-combos from the policy, generate
image-sets on Modal, score them on Modal, and shove the policy toward the
elements that the winning sets share (multiplicative-weights / "Hedge" update).
Run it and watch it converge.

    $ python loop.py

ALL shared shapes, vocab, config, and hyperparameters live in contracts.py —
this module redefines none of them.

----------------------------------------------------------------------------------
SWAPPING IN THE REAL GENERATE / JUDGE  (Modal-backed, two independent flags)
----------------------------------------------------------------------------------
The loop runs locally, but each iteration dispatches generation and judging to
Modal: the real generate()/judge() call `.map()` internally so the work fans out
across containers. Two independent flags let you mock either side for local tests:

    python loop.py                            # both mocked (default, fully local)
    USE_MOCK=0 python loop.py                 # real Modal generation, mock judge
    USE_MOCK_JUDGE=0 python loop.py           # mock generation, real Modal judge
    USE_MOCK=0 USE_MOCK_JUDGE=0 python loop.py # full Modal pipeline

    real modules / names (see contracts.py):
        generate.py  ->  generate(prompts: list[str]) -> list[list[str]]   # .map()
        judge.py     ->  judge(sets: list[ContentSet]) -> dict[str, JudgeResult]  # .map()
"""

import os
import glob
import json
import math
import time
import random
import tempfile

import obs  # Raindrop Workshop observability; self-gates on USE_RAINDROP (safe when 0)


def _envf(name: str, default) -> float:
    """NEVER-RAISE numeric env parse: returns float(env[name]) or float(default).

    A non-empty but non-numeric value (e.g. JUDGE_BUDGET_SECS=abc) must NOT throw
    at module import — that would die before run_loop can write any state.json,
    violating NEVER-RAISE. `os.environ.get(name) or default` only catches empty/
    unset; the float() still throws on 'abc', so we catch it and fall back."""
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return float(default)

from contracts import (
    ELEMENTS, VERTICAL, TRENDING, STATE_PATH, MOCK_TARGET,
    N_ITER, N_SETS, K_ELEMENTS, IMAGES_PER_SET, ETA, TOP_M, EPS,
    DEMO_DEADLINE_SECS, MAX_VIDEOS,
    ContentSet, JudgeResult, Iteration, State,
)

# state.json lives next to this file regardless of CWD
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), STATE_PATH)

# Fixed seed so the demo converges the same way every run; override with LOOP_SEED.
# Seed 2 (tuned for N_SETS=3 / N_ITER=3): best rises 4.67->7.33->7.33, avg climbs
# 2.89->4.67->6.44, and policy mass concentrates on the 3 hidden-target elements.
random.seed(int(os.environ.get("LOOP_SEED", "2")))


# ---------------------------------------------------------------------------
# 1. policy init
# ---------------------------------------------------------------------------
def init_policy() -> dict:
    """Uniform weights over the 12 elements, summing to 1.0."""
    w = 1.0 / len(ELEMENTS)
    return {el: w for el in ELEMENTS}


# ---------------------------------------------------------------------------
# 2. weighted sampling WITHOUT replacement
# ---------------------------------------------------------------------------
def sample_set_elements(policy: dict, k: int = K_ELEMENTS) -> list:
    """Sample k distinct elements, weighted by policy, without replacement."""
    els = list(policy.keys())
    weights = [max(policy[e], 0.0) for e in els]
    k = min(k, len(els))
    chosen = []
    for _ in range(k):
        total = sum(weights)
        if total <= 0:                      # degenerate: fall back to uniform
            i = random.randrange(len(els))
        else:
            r = random.uniform(0, total)
            upto, i = 0.0, len(els) - 1
            for j, wt in enumerate(weights):
                upto += wt
                if upto >= r:
                    i = j
                    break
        chosen.append(els[i])
        del els[i]
        del weights[i]
    return chosen


# ---------------------------------------------------------------------------
# 3. prompt builder
# ---------------------------------------------------------------------------
def build_prompt(elements: list, trending: list) -> str:
    """Compose an image-gen prompt for one TikTok-dance visual-story slide."""
    topic = random.choice(trending)
    blend = ", ".join(elements)
    return (
        f"A vertical 9:16 TikTok dance visual-story slide. "
        f"Aesthetic: {blend}. "
        f"Riding the trend '{topic}'. "
        f"Dynamic dancers mid-move, bold composition, high energy, "
        f"social-ready, cohesive color story. No text overlays."
    )


# ---------------------------------------------------------------------------
# 4. Hedge (multiplicative-weights) policy update
# ---------------------------------------------------------------------------
def hedge_update(policy: dict, sets: list, eta: float = ETA,
                 top_m: int = TOP_M, eps: float = EPS) -> dict:
    """Move policy toward elements shared by the top_m highest-scoring sets."""
    if not sets:
        return dict(policy)

    top = sorted(sets, key=lambda s: s["score"], reverse=True)[:max(1, top_m)]
    n_top = len(top)

    # reward[el] = fraction of top sets that contain el
    reward = {}
    for el in policy:
        hits = sum(1 for s in top if el in s["elements"])
        reward[el] = hits / n_top

    # multiplicative update: w_i *= exp(eta * reward_i)
    new_w = {el: policy[el] * math.exp(eta * reward[el]) for el in policy}

    # renormalize -> sum 1.0
    total = sum(new_w.values())
    new_w = {el: w / total for el, w in new_w.items()}

    # enforce floor, then renormalize again
    new_w = {el: max(w, eps) for el, w in new_w.items()}
    total = sum(new_w.values())
    new_w = {el: w / total for el, w in new_w.items()}
    return new_w


# ===========================================================================
# STUBS — deterministic mocks so the loop runs standalone NOW
# ===========================================================================
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# Projected wall time the JUDGE fan-out can still add AFTER generate returns.
# Matches judge.py's per-call timeout (judge_one: timeout=120). Used by the
# mid-iteration wall-clock guard so iter 1 stops BEFORE dispatching a judge that
# would push past DEMO_DEADLINE_SECS (generate is capped to DEADLINE-90, so the
# bare `elapsed > DEADLINE` check alone can never catch the generate+judge case).
JUDGE_BUDGET_SECS = _envf("JUDGE_BUDGET_SECS", 120)


def _loop_cached_clip_refs() -> list:
    """Repo-root-relative paths to cached .mp4 clips under demo_cache/clips/.

    Loop-level fallback for when `from generate import GENERATE` failed (so we
    canNOT rely on generate.py's helpers being importable). Mirrors generate.py's
    playable-clip convention: returns real .mp4 paths the dashboard plays, never
    a data-URI/SVG. Returns [] if nothing is cached. Sorted with the canonical
    sample floated to the front for a sensible single-clip placeholder."""
    pattern = os.path.join(_REPO_ROOT, "demo_cache", "clips", "*.mp4")
    try:
        abs_paths = sorted(glob.glob(pattern))
    except Exception:                                # glob basically never raises
        abs_paths = []
    rels = [os.path.relpath(p, _REPO_ROOT) for p in abs_paths]
    rels.sort(key=lambda r: (os.path.basename(r) != "sample_seedance.mp4", r))
    return rels


# Resolved once at import; safe even when the dir is missing (-> []).
_LOOP_CACHED_CLIPS = _loop_cached_clip_refs()


def mock_generate(prompts: list) -> list:
    """Loop-level GENERATE fallback (fires only if generate.py won't import).

    Mirrors generate.local_generate: ONE playable cached .mp4 ref per prompt,
    cycled across demo_cache/clips/*.mp4 so each set gets a distinct clip. Never
    emits an SVG/data-URI (the dashboard rejects those). Falls back to '' when
    nothing is cached, which the dashboard safely renders as no-video."""
    clips = _LOOP_CACHED_CLIPS
    if not clips:
        return [[""] for _ in prompts]
    return [[clips[i % len(clips)]] for i, _ in enumerate(prompts)]


def mock_judge(sets: list) -> dict:
    """Score sets by overlap with MOCK_TARGET: score = 2 + 8*(overlap/k)."""
    out = {}
    for s in sets:
        els = s["elements"]
        k = max(1, len(els))
        overlap = len(set(els) & MOCK_TARGET)
        score = 2.0 + 8.0 * (overlap / k)
        out[s["id"]] = {"score": score, "coherent": score > 3.0}
    return out


# ---------------------------------------------------------------------------
# Resolve GENERATE / JUDGE  (two independent swap flags; real ones use Modal .map())
# ---------------------------------------------------------------------------
USE_MOCK = os.environ.get("USE_MOCK", "1") != "0"              # generation: free vs paid
USE_MOCK_JUDGE = os.environ.get("USE_MOCK_JUDGE", "1") != "0"  # judging

# Effective judge budget for the mid-iteration wall-clock guard. The real Modal
# judge fans out per-clip (~120s, see JUDGE_BUDGET_SECS), but mock_judge is a
# pure in-memory loop that costs ~0s — so projecting a 120s judge cost on the
# FREE mock path would wrongly trip the guard for any small DEMO_DEADLINE_SECS
# (e.g. a quick free smoke test), stopping iter 1 UN-judged with all-zero scores
# and breaking convergence. Charge nothing for the mock judge so the free path
# always runs to convergence; the real judge keeps its full projected budget.
JUDGE_BUDGET_EFFECTIVE = 0.0 if USE_MOCK_JUDGE else JUDGE_BUDGET_SECS

# generate.py REPLACES the old built-in mock_generate. It honors USE_MOCK itself:
#   USE_MOCK=1 / unset (default) -> free local SVG placeholders (zero cost)
#   USE_MOCK=0                   -> real parallel image fan-out on Modal
# mock_generate stays only as a fallback if generate.py / modal can't be imported.
os.environ["USE_MOCK"] = "1" if USE_MOCK else "0"             # normalize for generate.py
try:
    from generate import GENERATE                            # honors USE_MOCK internally
    GEN_BACKEND = "local" if USE_MOCK else "modal"
except Exception as _gen_exc:
    GENERATE = mock_generate                                 # fallback: generate.py/modal absent
    GEN_BACKEND = f"loop-mock({type(_gen_exc).__name__})"

if USE_MOCK_JUDGE:
    JUDGE = mock_judge
else:
    try:
        from judge import judge as JUDGE        # Modal-backed; calls .map() internally
    except Exception as _judge_exc:             # judge.py / modal absent: degrade, never crash
        JUDGE = mock_judge
        print(f"[loop] judge import FAILED {type(_judge_exc).__name__}: "
              f"{_judge_exc} — falling back to mock_judge")


# ---------------------------------------------------------------------------
# NEVER-RAISE wrappers around the two external calls. Any failure degrades to a
# placeholder/zeroed result + a loud log line so a bad container/API blip can
# never propagate an exception that kills the loop.
# ---------------------------------------------------------------------------
def _safe_generate(prompts: list) -> list:
    """GENERATE(prompts) -> list[list[str]]; on ANY failure, return one empty
    inner list per prompt (downstream maps that to video_url='')."""
    try:
        out = GENERATE(prompts)
    except Exception as exc:
        print(f"[loop] GENERATE raised {type(exc).__name__}: {exc} — "
              f"using {len(prompts)} empty placeholder lists")
        return [[] for _ in prompts]
    # normalize shape: must be one inner list per prompt
    if not isinstance(out, list) or len(out) != len(prompts):
        print(f"[loop] GENERATE returned unexpected shape "
              f"({type(out).__name__}, len={len(out) if isinstance(out, list) else 'n/a'}) "
              f"— padding to {len(prompts)} empty lists")
        out = [[] for _ in prompts]
    return [(inner if isinstance(inner, list) else []) for inner in out]


def _safe_judge(sets: list) -> dict:
    """JUDGE(sets) -> {id: {score, coherent}}; on ANY failure or missing id,
    degrade that verdict to a zeroed, incoherent result."""
    try:
        verdicts = JUDGE(sets)
    except Exception as exc:
        print(f"[loop] JUDGE raised {type(exc).__name__}: {exc} — zeroing all {len(sets)} sets")
        verdicts = {}
    if not isinstance(verdicts, dict):
        print(f"[loop] JUDGE returned {type(verdicts).__name__}, expected dict — zeroing all sets")
        verdicts = {}
    out: dict = {}
    for s in sets:
        v = verdicts.get(s["id"])
        if not isinstance(v, dict) or "score" not in v:
            print(f"[loop] JUDGE missing/invalid verdict for {s['id']} — zeroing it")
            out[s["id"]] = {"score": 0.0, "coherent": False}
        else:
            try:
                out[s["id"]] = {"score": float(v["score"]),
                                "coherent": bool(v.get("coherent", False))}
            except Exception:
                out[s["id"]] = {"score": 0.0, "coherent": False}
    return out


def _clip_ref(inner: list) -> str:
    """VIDEO_URL CONVENTION: a set's clip reference is element 0 of its inner
    GENERATE list (real Seedance result_url OR repo-root-relative mock path),
    or '' when absent/empty."""
    if isinstance(inner, list) and inner and isinstance(inner[0], str):
        return inner[0]
    return ""


# ---------------------------------------------------------------------------
# 5. the loop
# ---------------------------------------------------------------------------
def _write_state(state: dict) -> None:
    """Atomic write: dump to a temp file in the same dir, then os.replace().
    A reader (dashboard) never sees a half-written state.json. Never raises."""
    try:
        d = os.path.dirname(STATE_FILE) or "."
        fd, tmp = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, STATE_FILE)          # atomic on POSIX
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as exc:
        print(f"[loop] WARN _write_state failed {type(exc).__name__}: {exc}")


def _fmt_policy(policy: dict, top=5) -> str:
    items = sorted(policy.items(), key=lambda kv: kv[1], reverse=True)[:top]
    return "  ".join(f"{el}={w:.3f}" for el, w in items)


def run_loop(n_iter: int = N_ITER, n_sets: int = N_SETS, k: int = K_ELEMENTS) -> None:
    """Run the self-improving loop, writing State to STATE_PATH after each iter.

    Guards (env-tunable via contracts):
      * WALL-CLOCK — DEMO_DEADLINE_SECS (default 240): before starting each new
        iteration, project completion from elapsed/iters-done; if it would blow
        the deadline, STOP early and finalize. ALWAYS ends with a valid state.json.
      * CREDIT     — MAX_VIDEOS (default 12): real Seedance clips are billed, so
        cap total sets (≈clips) generated across the whole run.
    """
    policy = init_policy()

    # --- DEMO HOOK (gated, harmless): FORCE_MODE_COLLAPSE=1 skews the policy so
    # one element holds 0.7 and the other 11 share the remaining 0.3 (renormalized),
    # so the mode-collapse alarm (>0.6) trips on iter 1. Default behavior unchanged.
    if os.environ.get("FORCE_MODE_COLLAPSE") == "1":
        try:
            collapse_el = ELEMENTS[0]
            rest = [e for e in ELEMENTS if e != collapse_el]
            share = (1.0 - 0.7) / max(1, len(rest))
            policy = {e: (0.7 if e == collapse_el else share) for e in ELEMENTS}
            total = sum(policy.values()) or 1.0
            policy = {e: w / total for e, w in policy.items()}
            print(f"[loop] FORCE_MODE_COLLAPSE=1 -> skewed policy: {collapse_el}=0.700")
        except Exception as exc:
            print(f"[loop] FORCE_MODE_COLLAPSE skew failed {type(exc).__name__}: {exc}")

    # --- Raindrop: open one Interaction for the whole run (best-effort, never
    # raises; no-op when USE_RAINDROP=0). obs self-gates internally too.
    try:
        obs.begin_run(
            event="cringe-filter-loop",
            user_id="demo",
            input_str=f"{VERTICAL} | {n_iter}x{n_sets}",
        )
    except Exception as exc:
        print(f"[loop] obs.begin_run failed {type(exc).__name__}: {exc}")

    # Trending signal — computed ONCE at run start (NOT per iteration). Optionally
    # a LIVE YouTube pull; trending.py never raises and never blocks beyond ~2s,
    # degrading to contracts.TRENDING. If trending.py is absent the import fails
    # gracefully and we use the cached TRENDING list.
    try:
        from trending import get_trending
        trends = get_trending(VERTICAL) or list(TRENDING)
    except Exception:
        trends = list(TRENDING)

    state: State = {
        "vertical": VERTICAL,
        "trending": trends,
        "current_iteration": 0,
        "iterations": [],
    }

    # Seed the observability mirror ONCE (only when the feature is on). The
    # dashboard reads state["raindrop"]; when USE_RAINDROP=0 we never add the key.
    if obs.USE_RAINDROP:
        state["raindrop"] = {"workshop_url": obs.WORKSHOP_URL, "events": []}

    t_start = time.monotonic()                   # wall-clock guard anchor
    iters_done = 0
    videos_spent = 0                             # running count of clips generated
    stop_reason = "completed"
    best_history: list = []                      # best_score per iter (plateau alarm)
    best_overall = 0.0                           # for the finish_run summary

    # Optional simulated per-iteration cost (default 0): lets the FREE mock path
    # exercise the wall-clock guard (real iterations are genuinely ~105s/clip and
    # trip it on their own). Never affects the real path unless explicitly set.
    mock_iter_secs = _envf("MOCK_ITER_SECS", 0)

    print(f"vertical: {VERTICAL}   "
          f"generate={GEN_BACKEND}   "
          f"judge={'mock' if USE_MOCK_JUDGE else 'modal'}   "
          f"target(hidden)={'/'.join(sorted(MOCK_TARGET))}\n"
          f"guards: DEMO_DEADLINE_SECS={DEMO_DEADLINE_SECS}  MAX_VIDEOS={MAX_VIDEOS}\n")

    # write an initial (empty-but-valid) state so state.json is NEVER missing,
    # even if we stop before the very first iteration finishes.
    _write_state(state)

    for it in range(1, n_iter + 1):
        elapsed = time.monotonic() - t_start

        # --- WALL-CLOCK GUARD: project completion of THIS iteration before starting it.
        if iters_done > 0:
            avg_iter = elapsed / iters_done      # observed mean iteration wall time
            projected_done = elapsed + avg_iter  # finishing one more iteration
            if projected_done > DEMO_DEADLINE_SECS:
                stop_reason = "deadline"
                print(f"[loop] WALL-CLOCK STOP before iter {it}: elapsed={elapsed:.1f}s "
                      f"avg/iter={avg_iter:.1f}s projected={projected_done:.1f}s "
                      f"> DEMO_DEADLINE_SECS={DEMO_DEADLINE_SECS}")
                break
        elif elapsed > DEMO_DEADLINE_SECS:
            # no completed iter yet but we're already over budget (e.g. deadline≈0/1)
            stop_reason = "deadline"
            print(f"[loop] WALL-CLOCK STOP before iter {it}: elapsed={elapsed:.1f}s "
                  f"already > DEMO_DEADLINE_SECS={DEMO_DEADLINE_SECS}")
            break

        # --- CREDIT GUARD: each set generates ~one clip; don't exceed MAX_VIDEOS.
        n_sets_iter = n_sets
        if videos_spent + n_sets_iter > MAX_VIDEOS:
            n_sets_iter = MAX_VIDEOS - videos_spent
            print(f"[loop] CREDIT GUARD: trimming iter {it} to {n_sets_iter} set(s) "
                  f"(videos_spent={videos_spent}, MAX_VIDEOS={MAX_VIDEOS})")
        if n_sets_iter <= 0:
            stop_reason = "max_videos"
            print(f"[loop] CREDIT STOP before iter {it}: MAX_VIDEOS={MAX_VIDEOS} reached")
            break

        # build n_sets element-combos -> prompts
        combos = [sample_set_elements(policy, k) for _ in range(n_sets_iter)]
        prompts = [build_prompt(c, trends) for c in combos]

        # generate clip-sets (fans out across Modal containers when real) — never raises
        clips = _safe_generate(prompts)
        videos_spent += n_sets_iter

        # assemble sets (pre-score) — ids s0..s{n-1} per contracts.ContentSet.
        # VIDEO_URL FLOW: element 0 of each inner GENERATE list is the clip ref.
        sets: list[ContentSet] = []
        for i, (combo, inner) in enumerate(zip(combos, clips)):
            sets.append({
                "id": f"s{i}",
                "elements": combo,
                "score": 0.0,
                "coherent": False,
                "images": inner if isinstance(inner, list) else [],
                "video_url": _clip_ref(inner),
            })

        # --- WALL-CLOCK GUARD (mid-iteration): generation alone can eat the whole
        # budget on the real path (one clip polls for ~100s+). Re-check the deadline
        # AFTER generate returns and BEFORE dispatching judge so a single iteration
        # can't overshoot by also fanning out the judge. generate is capped to
        # DEADLINE-90 (generate.py), so a bare `elapsed > DEADLINE` check can NEVER
        # catch the generate+judge case — instead PROJECT the post-judge time and
        # stop if dispatching judge would blow the budget. Clips are already paid
        # for, so we KEEP these sets (zeroed/incoherent) in state.json — they carry
        # a playable video_url for the dashboard — then finalize and stop early.
        elapsed = time.monotonic() - t_start
        if elapsed + JUDGE_BUDGET_EFFECTIVE > DEMO_DEADLINE_SECS:
            stop_reason = "deadline"
            print(f"[loop] WALL-CLOCK STOP after generate in iter {it}: "
                  f"elapsed={elapsed:.1f}s + JUDGE_BUDGET_SECS={JUDGE_BUDGET_EFFECTIVE:.0f}s "
                  f"> DEMO_DEADLINE_SECS={DEMO_DEADLINE_SECS} "
                  f"— recording {len(sets)} generated set(s) un-judged and finalizing")
            iteration: Iteration = {
                "iter": it,
                "policy": policy,            # policy UNCHANGED (no judge -> no update)
                "best_score": 0.0,
                "avg_score": 0.0,
                "sets": sets,
            }
            state["iterations"].append(iteration)
            state["current_iteration"] = it
            _write_state(state)
            iters_done += 1
            break

        # capture policy-in BEFORE the Hedge update overwrites `policy`
        policy_in = dict(policy)

        # judge (fans out across Modal containers when real) — never raises
        verdicts = _safe_judge(sets)
        for s in sets:
            v = verdicts[s["id"]]
            s["score"] = float(v["score"])
            s["coherent"] = bool(v["coherent"])

        # update policy toward the winning elements (UNCHANGED math)
        policy = hedge_update(policy, sets)

        # record + persist (atomic write AFTER EVERY iteration)
        scores = [s["score"] for s in sets] or [0.0]
        best_score = max(scores)
        avg_score = sum(scores) / len(scores)
        iteration: Iteration = {
            "iter": it,
            "policy": policy,
            "best_score": best_score,
            "avg_score": avg_score,
            "sets": sets,
        }
        state["iterations"].append(iteration)
        state["current_iteration"] = it

        # =================================================================
        # RAINDROP OBSERVABILITY (additive, never-raises). Wrapped wholesale so
        # any obs hiccup can NEVER break the loop. Runs BEFORE _write_state so
        # the trace/alarm events land in the same state.json snapshot.
        # =================================================================
        try:
            best_history.append(best_score)
            best_overall = max(best_overall, best_score)

            # --- top movers / top elements for the span + trace detail ---
            def _top(pol, n=3):
                return sorted(pol.items(), key=lambda kv: kv[1], reverse=True)[:n]

            top_in = _top(policy_in)
            top_out = _top(policy)
            # biggest single-element weight gain across the Hedge update
            deltas = {e: policy.get(e, 0.0) - policy_in.get(e, 0.0) for e in policy}
            top_mover = max(deltas.items(), key=lambda kv: kv[1]) if deltas else ("", 0.0)

            clips_ok = sum(1 for s in sets if s.get("video_url"))
            clips_fail = len(sets) - clips_ok

            # --- one SDK span per iteration capturing the full pipeline state ---
            obs.span(f"iter-{it}", {
                "iter": it,
                "policy_in_top": [f"{e}={w:.3f}" for e, w in top_in],
                "prompts": prompts,
                "gen_clips_ok": clips_ok,
                "gen_clips_fail": clips_fail,
                "video_urls": [s.get("video_url", "") for s in sets],
                "judge_decisions": {
                    s["id"]: {"score": round(s["score"], 3),
                              "coherent": bool(s["coherent"])}
                    for s in sets
                },
                "hedge_top_mover": f"{top_mover[0]}+{top_mover[1]:.3f}",
                "policy_out_top": [f"{e}={w:.3f}" for e, w in top_out],
                "best_score": round(best_score, 3),
                "avg_score": round(avg_score, 3),
            })

            # --- TRACE event (always, one per iter) ---
            top_str = ", ".join(f"{e}={w:.2f}" for e, w in top_out)
            obs.mirror(
                state, it, "trace", f"iter-{it}",
                f"best={best_score:.2f} avg={avg_score:.2f} top={top_str}",
                "info",
            )

            # --- ALARM: plateau (>=3 best scores; last 2 deltas both < 0.2) ---
            b = best_history
            if len(b) >= 3 and abs(b[-1] - b[-2]) < 0.2 and abs(b[-2] - b[-3]) < 0.2:
                obs.mirror(state, it, "alarm", "plateau",
                           "best_score flat (<0.2) for 2 iters", "warn")

            # --- ALARM: mode-collapse (any single policy weight > 0.6) ---
            collapsed = [(e, w) for e, w in policy.items() if w > 0.6]
            if collapsed:
                ce, cw = max(collapsed, key=lambda kv: kv[1])
                obs.mirror(state, it, "alarm", "mode-collapse",
                           f"policy collapsed: {ce}={cw:.2f} > 0.6 at iter {it}",
                           "warn")

            # --- ALARM: judge-degradation (>1/3 of sets hard-zeroed: score<=0.01) ---
            if n_sets_iter > 0:
                zeroed = sum(1 for s in sets if s["score"] <= 0.01)
                if zeroed / n_sets_iter > (1.0 / 3.0):
                    obs.mirror(
                        state, it, "alarm", "judge-degradation",
                        f"{zeroed}/{n_sets_iter} sets hard-zeroed "
                        f"(>1/3 fell back to mock) at iter {it}",
                        "warn",
                    )
        except Exception as exc:  # obs already self-guards; defensive belt-and-braces
            print(f"[loop] obs instrumentation failed {type(exc).__name__}: {exc} "
                  f"— loop continues")

        _write_state(state)

        iters_done += 1

        print(f"iter {it}:  best={best_score:5.2f}  avg={avg_score:5.2f}  "
              f"clips={videos_spent}  elapsed={time.monotonic() - t_start:5.1f}s  "
              f"| top policy: {_fmt_policy(policy)}")

        if mock_iter_secs > 0:                    # simulated cost for guard testing only
            time.sleep(mock_iter_secs)

    # FINAL write — guarantees a valid state.json on every exit path.
    _write_state(state)

    # --- Raindrop: close the run on EVERY exit path (completed / deadline /
    # max_videos / early-stop). Best-effort; obs self-guards and never raises.
    try:
        obs.finish_run(
            output=f"done: stop={stop_reason} iters={iters_done} best={best_overall:.2f}"
        )
    except Exception as exc:
        print(f"[loop] obs.finish_run failed {type(exc).__name__}: {exc}")

    # convergence summary
    print(f"\nstopped: {stop_reason}  iters_done={iters_done}  clips={videos_spent}  "
          f"wall={time.monotonic() - t_start:.1f}s")
    print("converged policy (target elements):")
    for el in sorted(MOCK_TARGET):
        print(f"  {el:<16} {policy[el]:.3f}")
    print(f"\nwrote {STATE_FILE}")


if __name__ == "__main__":
    run_loop()
