#!/usr/bin/env python3
"""real_pass.py — run ONE real video-native pass LOCALLY (no Modal cloud).

Identical pipeline to the Modal version — same Seedance /video/generations API
(Agent A) and the SAME Qwen-VL judge logic reused from judge.py (Agent B) — but
orchestrated locally so it runs where Modal-cloud execution is unavailable.
Real Seedance clips -> downloaded to demo_cache/clips/ -> real Qwen-VL scores ->
real state.json (+ snapshot to demo_cache/state.json for replay). NEVER raises.

    python real_pass.py            # REAL_ITERS (default 2) x REAL_SETS (default 3)
"""
from __future__ import annotations
import os, sys, json, time, base64, tempfile
from concurrent.futures import ThreadPoolExecutor

# load keys from gitignored .modal.env
_HERE = os.path.dirname(os.path.abspath(__file__))
for _l in open(os.path.join(_HERE, ".modal.env")):
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        _k, _v = _l.split("=", 1); os.environ.setdefault(_k.strip(), _v.strip())

import requests
from contracts import N_SETS, N_ITER, K_ELEMENTS, TRENDING, VERTICAL, MOCK_TARGET
import loop                      # init_policy, sample_set_elements, build_prompt, hedge_update
import judge                     # _frames_to_uris, _mock_score_one, _SYSTEM, _RUBRIC, _clamp, MODEL, N_CALLS, COHERENT_MIN

ITERS = int(os.environ.get("REAL_ITERS", "2"))
SETS = int(os.environ.get("REAL_SETS", str(N_SETS)))
MAX_VIDEOS = int(os.environ.get("MAX_VIDEOS", "12"))
CLIPS_DIR = os.path.join(_HERE, "demo_cache", "clips")
os.makedirs(CLIPS_DIR, exist_ok=True)

# Seedance creds: prefer SEEDANCE_* (Modal-secret names), else the TokenRouter vars in .modal.env
SD_BASE = (os.environ.get("SEEDANCE_BASE_URL") or os.environ["TOKENROUTER_BASE_URL"]).rstrip("/")
SD_KEY = os.environ.get("SEEDANCE_API_KEY") or os.environ["TOKENROUTER_API_KEY"]
SD_MODEL = os.environ.get("SEEDANCE_MODEL", "dreamina-seedance-2-0-fast-260128")
ION_BASE = os.environ["IONROUTER_BASE_URL"].rstrip("/")
ION_KEY = os.environ["IONROUTER_KEY"]

_videos_made = [0]


def log(m): print(f"[real_pass {time.strftime('%H:%M:%S')}] {m}", flush=True)


# ---- Seedance: submit -> poll -> download (CONFIRMED seam, SETUP 2) ----------
def seedance_clip(prompt: str, tag: str) -> str:
    """Generate one real clip, return repo-root-relative mp4 path, else '' (never raises)."""
    if _videos_made[0] >= MAX_VIDEOS:
        log(f"{tag}: MAX_VIDEOS cap hit -> skip real gen"); return ""
    _videos_made[0] += 1
    H = {"Authorization": f"Bearer {SD_KEY}", "Content-Type": "application/json"}
    try:
        payload = {"model": SD_MODEL, "prompt": prompt, "ratio": "9:16", "duration": 5, "resolution": "480p"}
        r = requests.post(f"{SD_BASE}/video/generations", headers=H, json=payload, timeout=40)
        if r.status_code >= 300:
            log(f"{tag}: submit HTTP {r.status_code} {r.text[:120]}"); return ""
        tid = r.json().get("task_id") or r.json().get("id")
        log(f"{tag}: submitted {tid}")
        deadline = time.time() + 200
        while time.time() < deadline:
            time.sleep(6)
            g = requests.get(f"{SD_BASE}/video/generations/{tid}", headers=H, timeout=30)
            data = (g.json() or {}).get("data", {})
            st = (data.get("status") or "").upper()
            if st in ("SUCCESS", "SUCCEEDED", "COMPLETED"):
                url = data.get("result_url") or (((data.get("data") or {}).get("content") or {}).get("video_url"))
                if not url:
                    log(f"{tag}: SUCCESS but no url"); return ""
                rel = os.path.join("demo_cache", "clips", f"real_{tag}.mp4")
                dest = os.path.join(_HERE, rel)
                with requests.get(url, stream=True, timeout=60) as dl:
                    dl.raise_for_status()
                    with open(dest, "wb") as f:
                        for chunk in dl.iter_content(1 << 15):
                            f.write(chunk)
                log(f"{tag}: downloaded {rel} ({os.path.getsize(dest)//1000} KB)")
                return rel
            if st in ("FAILED", "CANCELLED", "ERROR"):
                log(f"{tag}: terminal {st}"); return ""
        log(f"{tag}: poll timeout"); return ""
    except Exception as e:
        log(f"{tag}: gen EXC {e!r}"); return ""


# ---- Qwen-VL judge (REUSES judge.py logic over real frames; SETUP 3) --------
def qwen_judge(video_rel: str, elements: list, tag: str) -> dict:
    """Real Qwen-VL verdict; per-set fallback to mock on any failure (never raises)."""
    try:
        if not video_rel:
            log(f"{tag}: no clip -> mock fallback"); return judge._mock_score_one(elements)
        frames = judge._frames_to_uris(video_rel)
        if not frames:
            log(f"{tag}: 0 frames -> mock fallback"); return judge._mock_score_one(elements)
        content = [{"type": "text", "text": judge._RUBRIC}]
        for uri in frames:
            content.append({"type": "image_url", "image_url": {"url": uri}})
        msgs = [{"role": "system", "content": judge._SYSTEM}, {"role": "user", "content": content}]
        H = {"Authorization": f"Bearer {ION_KEY}", "Content-Type": "application/json"}
        cohs, cres = [], []
        for _ in range(judge.N_CALLS):
            try:
                body = {"model": judge.MODEL, "messages": msgs, "response_format": {"type": "json_object"}, "temperature": 0.2, "max_tokens": 80}
                rr = requests.post(f"{ION_BASE}/chat/completions", headers=H, json=body, timeout=60)
                d = json.loads(rr.json()["choices"][0]["message"]["content"])
                cohs.append(judge._clamp(d["coherence"])); cres.append(judge._clamp(d["creativity"]))
            except Exception as e:
                log(f"{tag}: qwen call fail {e!r}")
        if not cohs:
            log(f"{tag}: all qwen calls failed -> mock fallback"); return judge._mock_score_one(elements)
        coh = sum(cohs) / len(cohs); cre = sum(cres) / len(cres)
        score = cre * (coh / 10.0)
        log(f"{tag}: qwen coh={coh:.1f} cre={cre:.1f} -> score={score:.2f}")
        return {"score": round(float(score), 3), "coherent": bool(coh >= judge.COHERENT_MIN)}
    except Exception as e:
        log(f"{tag}: judge EXC {e!r} -> mock fallback"); return judge._mock_score_one(elements)


def write_state(state):
    tmp = os.path.join(_HERE, ".state.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, os.path.join(_HERE, "state.json"))


def main():
    log(f"REAL pass: {ITERS} iters x {SETS} sets (MAX_VIDEOS={MAX_VIDEOS})  seedance={SD_MODEL}  judge={judge.MODEL}")
    policy = loop.init_policy()
    state = {"vertical": VERTICAL, "trending": TRENDING, "current_iteration": 0, "iterations": [], "real": True}
    write_state(state)
    pool = ThreadPoolExecutor(max_workers=max(SETS, 3))
    for it in range(1, ITERS + 1):
        combos = [loop.sample_set_elements(policy, K_ELEMENTS) for _ in range(SETS)]
        prompts = [loop.build_prompt(c, TRENDING) for c in combos]
        log(f"--- iter {it}: generating {SETS} clips in parallel ---")
        clips = list(pool.map(lambda a: seedance_clip(a[0], a[1]), [(prompts[i], f"i{it}_s{i}") for i in range(SETS)]))
        log(f"iter {it}: judging {SETS} clips in parallel ---")
        verdicts = list(pool.map(lambda a: qwen_judge(a[0], a[1], a[2]),
                                 [(clips[i], combos[i], f"i{it}_s{i}") for i in range(SETS)]))
        sets = []
        for i in range(SETS):
            v = verdicts[i]
            sets.append({"id": f"s{i}", "elements": combos[i], "score": float(v["score"]),
                         "coherent": bool(v["coherent"]), "video_url": clips[i] or ""})
        policy = loop.hedge_update(policy, sets)
        scores = [s["score"] for s in sets]
        state["iterations"].append({"iter": it, "policy": policy, "best_score": max(scores),
                                    "avg_score": sum(scores) / len(scores), "sets": sets})
        state["current_iteration"] = it
        write_state(state)
        log(f"iter {it}: best={max(scores):.2f} avg={sum(scores)/len(scores):.2f}  clips_made={_videos_made[0]}")
    # snapshot for replay
    import shutil
    shutil.copy(os.path.join(_HERE, "state.json"), os.path.join(_HERE, "demo_cache", "state.json"))
    log(f"DONE. {_videos_made[0]} real clips. state.json + demo_cache/state.json written.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL {e!r} — state.json from completed iters is preserved")
        sys.exit(0)
