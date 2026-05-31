"""
contracts.py — shared definitions for the self-improving content loop.
A (generate.py), B (judge.py), C (loop.py), and the dashboard ALL import from here.
Do not redefine these anywhere else. If a shape changes, change it HERE only.
"""
from __future__ import annotations
import os
from typing import TypedDict, NotRequired

# ---- Fixed creative vocabulary (12 elements) -------------------------------
ELEMENTS: list[str] = [
    "90s", "high-school", "emo", "rock-and-roll", "comedy", "dark",
    "dance-challenge", "slow-mo", "neon", "retro-film", "wholesome", "chaotic",
]

# ---- Demo config -----------------------------------------------------------
VERTICAL: str = "TikTok dance"
TRENDING: list[str] = [
    "Love and Affection challenge",
    "Bossman Dlow - Motion Party",
    "Captain Pineapple Bye Bye dance",
    "Tame Impala - Dracula",
    "Philippines 2-person dance mashup",
    "Jersey Club remix",
]

# Loop hyperparameters (tune at integration)
N_ITER: int = 3        # iterations per run (video-native demo)
N_SETS: int = 3        # candidate sets per iteration (video-native demo)
K_ELEMENTS: int = 3    # elements sampled per set
IMAGES_PER_SET: int = 2  # images generated per set
ETA: float = 0.7       # Hedge learning rate (bigger = faster, cruder convergence)
TOP_M: int = 2         # how many top sets define the "winners"
EPS: float = 0.02      # min weight floor per element (exploration)

# ---- Wall-clock + credit guards (read from env) ----------------------------
# The video-native loop dispatches real Seedance clips (~105s each), so a full
# run can blow past a demo budget. These two guards let run_loop stop early but
# still finalize a valid state.json, and cap total clips spent.
#
# NEVER-RAISE: contracts.py is imported by loop.py, generate.py, judge.py and the
# demo, so a junk env value (e.g. DEMO_DEADLINE_SECS=foo, MAX_VIDEOS=12.5) must
# NOT traceback at import — that would crash the whole pipeline before any
# state.json/guard logic runs. Parse defensively and fall back to the default.
def _envi(name: str, d: int) -> int:
    try:
        return int(float(os.environ.get(name) or d))
    except (TypeError, ValueError):
        return int(d)

DEMO_DEADLINE_SECS: int = _envi("DEMO_DEADLINE_SECS", 240)
MAX_VIDEOS: int = _envi("MAX_VIDEOS", 12)

# Shared file the loop WRITES and the dashboard READS
STATE_PATH: str = "state.json"

# Hidden target used ONLY by the mock generator/judge so the loop visibly
# converges during testing. The real judge ignores this.
MOCK_TARGET: set[str] = {"comedy", "dance-challenge", "90s"}


# ---- Data shapes (the contracts everything agrees on) ----------------------
class ContentSet(TypedDict, total=False):
    id: str                 # e.g. "s0"
    elements: list[str]     # subset of ELEMENTS, length K_ELEMENTS
    score: float            # 0-10, filled by the judge
    coherent: bool          # judge's gibberish gate
    images: list[str]       # base64 data-URIs, length IMAGES_PER_SET
    video_url: str          # OPTIONAL clip reference: absolute https Seedance
                            # result_url, OR a repo-root-relative path like
                            # demo_cache/clips/foo.mp4. Default '' when absent.

class JudgeResult(TypedDict):
    score: float            # 0-10
    coherent: bool

class Iteration(TypedDict):
    iter: int
    policy: dict[str, float]      # element -> weight, sums to ~1.0
    best_score: float
    avg_score: float
    sets: list[ContentSet]

# ---- Raindrop Workshop observability mirror (STEP 0 contract) --------------
# Additive: backend WRITES state["raindrop"], dashboard READS it. The state.json
# mirror is independent of the Raindrop SDK so the demo banner is bulletproof.
class RaindropEvent(TypedDict):
    iter: int
    type: str          # "trace" | "alarm"
    label: str
    detail: str
    severity: str      # "info" | "warn"

class Raindrop(TypedDict):
    workshop_url: str                  # e.g. "http://localhost:5899"
    events: list[RaindropEvent]        # appended newest-last

class State(TypedDict):
    vertical: str
    trending: list[str]
    current_iteration: int
    iterations: list[Iteration]
    raindrop: NotRequired[Raindrop]    # optional observability mirror


# ---- Function contracts (signatures everyone codes to) ---------------------
# A — generate.py:
#     generate(prompts: list[str]) -> list[list[str]]
#         returns one list of image data-URIs per prompt.
#
# B — judge.py:
#     judge(sets: list[ContentSet]) -> dict[str, JudgeResult]
#         keyed by set id.
#
# C — loop.py:
#     run_loop(n_iter=N_ITER, n_sets=N_SETS, k=K_ELEMENTS) -> None
#         writes State to STATE_PATH after every iteration.
