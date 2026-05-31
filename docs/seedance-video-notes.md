# Seedance video generation — learnings (ported from `/Users/mei/seeddance`)

Distilled from the working PersonalCast project (76 clips shipped). Source of truth:
`/Users/mei/seeddance/seedance/client.py` + `/Users/mei/seeddance/docs/api-notes.md`.

> **Correction up front:** video does **NOT** go through IonRouter / `api.imarouter.com`.
> That key is LLM-only (prompt refinement). Video goes **directly to BytePlus ModelArk
> (ByteDance)**. Target ModelArk to replicate video gen.

## API surface
- **Base URL (intl):** `https://ark.ap-southeast.bytepluses.com/api/v3`
  (China = `https://ark.cn-beijing.volces.com/api/v3`, *separate non-compatible key space*).
- **Submit:** `POST {base}/contents/generations/tasks`
- **Poll:** `GET {base}/contents/generations/tasks/{id}`
- **Key check (free, no credits):** `GET {base}/models`
- **Auth:** `Authorization: Bearer <SEEDANCE_API_KEY>` + `Content-Type: application/json`
- **Env:** `SEEDANCE_API_KEY` (or `SEEDANCE_API_KEYS` comma-list for rotation),
  `SEEDANCE_BASE_URL`, `SEEDANCE_MODEL`.
- **Model id:** `dreamina-seedance-2-0-fast-260128` (fast/cheap — all 76 prod clips used it).
  Full quality = `dreamina-seedance-2-0-260128`. `-260128` is a version date; confirm in console.

## Request shape (async submit → poll)
Resolution/ratio/duration are **inline `--` directives at the TAIL of the prompt text**,
not top-level JSON fields. Top-level is only `model`, `content`, `generate_audio`.
```json
{
  "model": "dreamina-seedance-2-0-fast-260128",
  "content": [
    {"type": "text", "text": "<dense cinematic prompt> --ratio 16:9 --duration 10 --resolution 720p"}
  ],
  "generate_audio": false
}
```
- `--ratio` ∈ `16:9|9:16|1:1`  ·  `--duration` int sec (3–15)  ·  `--resolution` ∈ `480p|720p|1080p`
- `generate_audio: true` → native audio + **lip-sync** when the prompt has **dialogue in
  double quotes**. (No external TTS needed.)
- **Image-to-video:** append a 2nd content item `{"type":"image_url","image_url":{"url": "<PUBLIC URL>"}}`.
  Must be a **public URL** (no base64/`file://`). `seeddance/scripts/upload_image.py` uploads a
  local PNG to 0x0.st (catbox.moe fallback) → URL. *Coded + documented but NOT used in prod
  (text-only shipped all 76 clips) — treat as plausible-but-unverified.*
- **Not supported by the client:** `fps`, `seed`, `negative_prompt`, `--style` (appears in one
  prose doc but the client never emits it).

## Polling / latency
- Submit → `{"id", "status":"queued"}`. Poll every **5s**. Terminal: `succeeded|failed|cancelled`.
- Succeeded response has `content.video_url` → **pre-signed `.mp4` on Volcengine TOS, expires 24h
  (download immediately)**. Plain streamed GET, no auth needed on the signed URL.
- **Real latency (76 clips): min 73s, median ~109s, max 308s.** Budget ~2 min/clip, up to ~5.

## Cost / limits
- ~**$0.30/clip** (anecdotal — no official rate in repo). Fast model is the cheaper tier.
- Max duration ~**12s** (multi-beat coherence) / 15s (single subject) on fast. Max res 1080p (720p default).
- No documented rate limit; mitigate with **multi-key rotation** + 1-in-flight-per-key semaphore.

## #1 gotcha — billing ≠ auth
`AccountOverdueError` **(HTTP 403)**: keys pass `GET /models` (200) but **task submission 403s**
because the shared parent billing account is overdue — blocks *all* keys from that account.
**A 200 on `/models` does NOT mean you can generate.** Other failures: `AuthenticationError` 401
(wrong region/key space), malformed keys (validate first), audio moderation on trademark mentions.
Error envelope is OpenAI-style `{"error":{"code","message",...}}`; `message` carries a `Request id:`
— always log it.

## Winning prompt structure
One dense paragraph (~60–150 words), ingredients in order, **directives last**:
1. shot type + framing → 2. subject w/ physical specifics → 3. wardrobe/setting → 4. **explicit
camera move** (e.g. "slow push-in at 50mm") → 5. **lighting** → 6. demeanor → 7. **dialogue in
double quotes** (lip-sync trigger) → 8. micro-movements → 9. `--ratio --duration --resolution`.
**Avoid:** on-screen text/logos (render as gibberish), >3 foreground subjects, deforming face
close-ups, vague filler prose (the `payload_bad.json` anti-pattern: abstract verbiage, no visuals).

## Minimal port (requests + stdlib)
```python
import os, time, requests
from pathlib import Path
BASE  = os.environ.get("SEEDANCE_BASE_URL", "https://ark.ap-southeast.bytepluses.com/api/v3").rstrip("/")
MODEL = os.environ.get("SEEDANCE_MODEL", "dreamina-seedance-2-0-fast-260128")
H     = {"Authorization": f"Bearer {os.environ['SEEDANCE_API_KEY']}", "Content-Type": "application/json"}

def generate_video(prompt, dest, *, ratio="16:9", duration=5, resolution="720p",
                   generate_audio=False, anchor_url=None, poll=5.0, max_wait=600.0):
    text = f"{prompt} --ratio {ratio} --duration {duration} --resolution {resolution}"
    content = [{"type": "text", "text": text}]
    if anchor_url:
        content.append({"type": "image_url", "image_url": {"url": anchor_url}})
    body = {"model": MODEL, "content": content, "generate_audio": generate_audio}
    r = requests.post(f"{BASE}/contents/generations/tasks", headers=H, json=body, timeout=60)
    if r.status_code >= 400:
        e = r.json().get("error", {}); raise RuntimeError(f"submit {r.status_code} {e.get('code')}: {e.get('message')}")
    tid = r.json()["id"]; deadline = time.monotonic() + max_wait
    while True:
        d = requests.get(f"{BASE}/contents/generations/tasks/{tid}", headers=H, timeout=60).json()
        s = d.get("status")
        if s == "succeeded": url = (d.get("content") or {}).get("video_url"); break
        if s in {"failed", "cancelled"}: raise RuntimeError(f"{tid} {s}: {d}")
        if time.monotonic() >= deadline: raise RuntimeError(f"timeout, last={s}")
        time.sleep(poll)
    out = Path(dest); out.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=60) as v:
        v.raise_for_status()
        with out.open("wb") as f:
            for chunk in v.iter_content(1 << 15): f.write(chunk)
    return str(out)
```

## How this fits THIS pipeline
- **Too slow/costly for inside the loop** (5 sets × 8 iters × ~2 min × $0.30 = ~80 min / ~$12).
- Natural fit: a **final "hero render"** — after `loop.py` converges, take the winning set's
  element-combo, build a Seedance prompt (reuse `build_prompt` + the structure above), and render
  ONE video. Optionally seed it with the winning set's generated image as an **anchor** (image→video).
- Modal shape: mirror `judge.py`/`generate.py` — a `video.py` app whose function submits+polls
  ModelArk (long `timeout`, since clips take minutes), driven via `with app.run()`. Add a
  `seedance-secret` Modal Secret with `SEEDANCE_API_KEY`. Keep a `USE_MOCK` local stub.
```
modal secret create seedance-secret SEEDANCE_API_KEY=<key>
```
