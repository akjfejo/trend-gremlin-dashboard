# SETUP findings — the SEAM (confirmed live, 2026-05-30)

Single source of truth for Agents A/B. All confirmed with real calls.

## SETUP 1 — Modal secrets (profile `hackathon-modal`)
- `openai-secret`        : OPENAI_API_KEY  (<openai-key>)  ✓ created
- `seedance-secret`      : SEEDANCE_API_KEY=<tokenrouter>, SEEDANCE_BASE_URL=https://api.tokenrouter.com/v1,
                           SEEDANCE_MODEL=dreamina-seedance-2-0-fast-260128,
                           IMAROUTER_API_KEY, IMAROUTER_BASE_URL=https://api.imarouter.com, IMAROUTER_MODEL=seedance-2.0-fast-cn  ✓
- `ionrouter-secret`     : IONROUTER_KEY (<ionrouter-key>), IONROUTER_BASE_URL=https://api.ionrouter.io/v1  ✓
All keys also in gitignored `.modal.env` (auto-loaded for local `python` runs).

## SETUP 2 — Seedance VIDEO gen (Agent A)   PRIMARY: TokenRouter
NOTE: the BytePlus `/api/v3/contents/generations/tasks` task API is NOT proxied by the routers (404).
The routers expose an OpenAI-style **`/video/generations`** pair instead.

- **Submit**  `POST  https://api.tokenrouter.com/v1/video/generations`
  Header: `Authorization: Bearer $SEEDANCE_API_KEY`
  JSON  : `{"model":"dreamina-seedance-2-0-fast-260128","prompt":"<plain text, NO --flags>",
           "ratio":"9:16","duration":5,"resolution":"480p"}`
  → `{"task_id":"task_…","status":"queued","progress":0}`
  ⚠ Use ratio/duration/resolution as JSON FIELDS — inline `--duration` directives in the prompt are rejected
    ("duration not supported"). The fast model NORMALIZES to ~5s / 720p / 24fps / audio-on regardless, ratio honored.
- **Fetch**   `GET   https://api.tokenrouter.com/v1/video/generations/{task_id}`
  → `{"code":"success","data":{"status":"SUCCESS"|"IN_PROGRESS"|"FAILED","progress":"100%",
        "result_url":"https://…tos…volces.com/….mp4?X-Tos-…",  // <-- USE THIS
        "data":{"content":{"video_url":"…same url…"}}}}`
  Terminal status (UPPERCASE): `SUCCESS` / `FAILED` / `CANCELLED`. result_url is a presigned mp4, **expires 24h → download now**.
- **Timing** : ~100–110s per clip. FALLBACK: imarouter base `https://api.imarouter.com/v1`, model `seedance-2.0-fast-cn`.
- Seam OUTPUT for the contract: `video_url` = `data.result_url` (str). Inner GENERATE list carries `[video_url]`.

## SETUP 3 — Qwen-VL VIDEO judge (Agent B)   IonRouter, OpenAI-compatible
- Endpoint `POST https://api.ionrouter.io/v1/chat/completions`, `Authorization: Bearer $IONROUTER_KEY`
- **Model** : `qwen3-vl-30b-a3b`  (also: qwen3-vl-8b, qwen2.5-vl-7b). imarouter has NO qwen-vl.
- CONFIRMED: accepts `image_url` content parts + `response_format={"type":"json_object"}`; returned strict
  `{"coherence":5,"creativity":1}`.  It does NOT take a raw mp4 URL — **SEAM ADAPTER**: download mp4 → ffmpeg
  sample K≈3 frames → base64 PNG data-URIs → send as multiple `image_url` parts. On any adapter/format failure,
  fall back to the mock judge FOR THAT SET and log loudly.

## Implications for guards
- 3 iters × 3 sets × ~105s ≫ DEMO_DEADLINE_SECS(240). Wall-clock guard WILL trigger → stop early, write valid state.json.
- MAX_VIDEOS(12) ≥ 9 needed; binding constraint is TIME, not credits. CACHE the real run; DEMO_REPLAY serves it.
