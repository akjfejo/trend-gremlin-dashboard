#!/usr/bin/env python3
"""
serve_dashboard.py — tiny stdlib http.server for the video-native TikTok loop.

Serves the REPO ROOT so that everything the dashboard needs is reachable from
one origin with a plain <video src> / fetch():
    /dashboard.html             the page
    /state.json                 live loop output (Agent C writes it)
    /demo_cache/clips/*.mp4      cached clips (mock + DEMO_REPLAY)
    /demo_cache/state.json       cached replay state (optional)

Both VIDEO_URL forms work unchanged:
  - absolute https URL  -> the browser fetches it directly (CORS permitting)
  - repo-root-relative  -> resolves against this server's root (demo_cache/clips/foo.mp4)

DEMO_REPLAY=1
  Pure replay: ZERO new API calls. The page is pointed at /demo_cache/state.json,
  which the server SYNTHESIZES on the fly from a frozen snapshot (a real
  demo_cache/state.json if shipped) or the last live run — rewriting EVERY clip
  ref to a local demo_cache/clips/*.mp4 so no expired presigned https Seedance
  result_url ever reaches the page. A REPLAY badge is shown and clips are served
  straight from demo_cache/clips/. Truly self-contained: no static cache file or
  API call required.

NEVER-RAISE: every request is wrapped; a handler failure degrades to a 500/empty
body + a loud log line and the server keeps running. Nothing here calls an
external API or spends credits.

Usage:
    python serve_dashboard.py                 # live, port 8000
    DEMO_REPLAY=1 python serve_dashboard.py   # replay badge + cached state
    PORT=8123 python serve_dashboard.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# Repo root = directory of this file, regardless of CWD.
ROOT = os.path.dirname(os.path.abspath(__file__))

PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "127.0.0.1")
REPLAY = os.environ.get("DEMO_REPLAY", "0") in ("1", "true", "True", "yes")


CLIPS_DIR = os.path.join(ROOT, "demo_cache", "clips")
CLIPS_REL = "demo_cache/clips"


def _list_cached_clips() -> list[str]:
    """Basenames of every playable clip we have on disk. NEVER-RAISE -> []."""
    try:
        return sorted(
            n for n in os.listdir(CLIPS_DIR)
            if n.lower().endswith((".mp4", ".webm", ".mov", ".m4v"))
        )
    except Exception as exc:  # NEVER-RAISE (dir missing, perms, …)
        print(f"[serve] WARN could not list {CLIPS_REL}: {exc}")
        return []


def _local_clip_for(ref, fallbacks, idx):
    """Map ANY video ref to a local demo_cache/clips/*.mp4 that exists on disk.

    Replay must be self-contained & API-free, so absolute http(s) Seedance
    result_url values (presigned, 24h-expired) are NEVER handed to the page.
    Rules (NEVER-RAISE — always returns a usable local ref if any clip exists):
      - already a repo-relative demo_cache/clips/<name> that exists -> keep it
      - else derive a basename and use the matching cached clip if present
      - else round-robin over whatever clips we shipped so <video> still plays
    """
    try:
        if not fallbacks:
            return ref  # nothing cached; leave as-is rather than blank it
        if isinstance(ref, str) and ref:
            base = ref.split("?", 1)[0].split("#", 1)[0]
            name = os.path.basename(base)
            # exact cached clip by basename
            if name in fallbacks:
                return f"{CLIPS_REL}/{name}"
            # already a local clips path that physically exists -> trust it
            low = base.lower()
            if (low.endswith((".mp4", ".webm", ".mov", ".m4v"))
                    and not re.match(r"^https?://", ref)
                    and os.path.isfile(os.path.join(ROOT, base))):
                return ref
        # absolute http(s), unknown, or missing-on-disk -> deterministic local clip
        return f"{CLIPS_REL}/{fallbacks[idx % len(fallbacks)]}"
    except Exception as exc:  # NEVER-RAISE
        print(f"[serve] WARN clip remap failed for {ref!r}: {exc}")
        return f"{CLIPS_REL}/{fallbacks[idx % len(fallbacks)]}" if fallbacks else ref


def _replay_state_bytes() -> bytes | None:
    """Build the SELF-CONTAINED replay state.json served at /demo_cache/state.json.

    Loads the source state (a real frozen demo_cache/state.json if one was
    shipped, else the live state.json from the last run) and rewrites EVERY
    video_url / images[] entry to a local, on-disk demo_cache/clips/*.mp4 so
    the <video> dashboard plays with ZERO API calls and no expired presigned
    https refs. NEVER-RAISE: returns None on any failure (caller falls back to
    serving the raw file), so replay degrades gracefully but never crashes.
    """
    clips = _list_cached_clips()
    cached = os.path.join(ROOT, "demo_cache", "state.json")
    live = os.path.join(ROOT, "state.json")
    src = cached if os.path.isfile(cached) else live
    try:
        with open(src, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as exc:  # NEVER-RAISE
        print(f"[serve] WARN could not load replay source {src!r}: {exc}")
        return None
    try:
        n = 0
        for it in (state.get("iterations") or []):
            for s in (it.get("sets") or []):
                local = _local_clip_for(s.get("video_url"), clips, n)
                s["video_url"] = local
                imgs = s.get("images")
                if isinstance(imgs, list) and imgs:
                    s["images"] = [local]
                else:
                    s["images"] = [local]
                n += 1
        return json.dumps(state, ensure_ascii=False).encode("utf-8")
    except Exception as exc:  # NEVER-RAISE
        print(f"[serve] WARN could not rewrite replay refs: {exc}")
        return None


def _state_url() -> str:
    """Which state.json the page should poll.

    In replay mode we always point the page at demo_cache/state.json, which the
    server SYNTHESIZES on the fly (see Handler._send_replay_state) from a frozen
    snapshot or the live run, with every clip ref remapped to a local
    demo_cache/clips/*.mp4. This makes replay self-contained and API-free even
    when the source carries expired presigned https Seedance result_url values.
    Returns a repo-root-relative path the browser can fetch directly.
    """
    if REPLAY:
        return "demo_cache/state.json"
    return "state.json"


# Built once at startup; injected to the page as /__config__.js
CONFIG_JS = (
    "window.__DEMO_REPLAY__ = {replay};\n"
    "window.__STATE_URL__ = {state!r};\n"
).format(replay=("true" if REPLAY else "false"), state=_state_url())


class Handler(SimpleHTTPRequestHandler):
    """Static file handler rooted at the repo, plus two tiny conveniences:
      - GET /            -> serves dashboard.html
      - GET /__config__.js -> injects replay flag + state url for the page
    Everything is wrapped so a single bad request never kills the server."""

    # Make relative clip paths and state.json resolve from the repo root.
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    # ---- never-raise request dispatch -------------------------------------
    def do_GET(self):  # noqa: N802 (stdlib naming)
        try:
            path = self.path.split("?", 1)[0].split("#", 1)[0]
            if path in ("/", "/index.html", "/dashboard"):
                self.path = "/dashboard.html"
                return super().do_GET()
            if path == "/__config__.js":
                return self._send_config()
            # In replay mode, synthesize a SELF-CONTAINED state.json whose clip
            # refs are all local demo_cache/clips/*.mp4 (no expired https refs).
            if REPLAY and path in ("/demo_cache/state.json", "/demo_cache/state.json/"):
                if self._send_replay_state():
                    return
                # synth failed -> fall through to static file (NEVER-RAISE)
            return super().do_GET()
        except BrokenPipeError:
            # client navigated away mid-stream (common with autoplay video) — ignore
            pass
        except Exception as exc:  # NEVER-RAISE
            print(f"[serve] ERROR handling GET {self.path!r}: "
                  f"{type(exc).__name__}: {exc}")
            try:
                self.send_error(500, "internal error (logged, server alive)")
            except Exception:
                pass

    def do_HEAD(self):  # noqa: N802
        try:
            return super().do_HEAD()
        except BrokenPipeError:
            pass
        except Exception as exc:  # NEVER-RAISE
            print(f"[serve] ERROR handling HEAD {self.path!r}: {exc}")

    def _send_config(self):
        body = CONFIG_JS.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_replay_state(self) -> bool:
        """Serve the synthesized self-contained replay state.json.

        Returns True if handled, False if synth failed (caller serves the static
        file instead). NEVER-RAISE."""
        try:
            body = _replay_state_bytes()
        except Exception as exc:  # NEVER-RAISE
            print(f"[serve] WARN replay-state synth raised: {exc}")
            body = None
        if body is None:
            return False
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.end_headers()
            self.wfile.write(body)
            return True
        except BrokenPipeError:
            return True  # client left mid-stream; we did handle it
        except Exception as exc:  # NEVER-RAISE
            print(f"[serve] WARN failed writing replay state: {exc}")
            return False

    # ---- HTTP Range support for <video> seeking ---------------------------
    # SimpleHTTPRequestHandler ignores Range and always 200s the whole file;
    # some browsers (notably Safari) won't play <video> without a 206. We add
    # minimal single-range support. NEVER-RAISE: any parse issue degrades to a
    # plain full-file 200 via the parent handler.
    def send_head(self):
        rng = self.headers.get("Range")
        if not rng or not rng.strip().lower().startswith("bytes="):
            return super().send_head()
        try:
            path = self.translate_path(self.path)
            if os.path.isdir(path) or not os.path.isfile(path):
                return super().send_head()
            size = os.path.getsize(path)
            spec = rng.split("=", 1)[1].split(",")[0].strip()
            start_s, _, end_s = spec.partition("-")
            if start_s == "":
                # suffix range: last N bytes
                length = int(end_s)
                start = max(0, size - length)
                end = size - 1
            else:
                start = int(start_s)
                end = int(end_s) if end_s else size - 1
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return None
            end = min(end, size - 1)
            f = open(path, "rb")
            f.seek(start)
            ctype = self.guess_type(path)
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            self._range_remaining = end - start + 1
            return f
        except Exception as exc:  # NEVER-RAISE -> plain full-file 200
            print(f"[serve] WARN range parse failed ({exc}) — serving full file")
            return super().send_head()

    def copyfile(self, source, outputfile):
        # Honor a single Range window if send_head set one.
        remaining = getattr(self, "_range_remaining", None)
        if remaining is None:
            return super().copyfile(source, outputfile)
        self._range_remaining = None
        try:
            while remaining > 0:
                chunk = source.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                outputfile.write(chunk)
                remaining -= len(chunk)
        except BrokenPipeError:
            pass  # client seeked away mid-stream

    # no-store so live polling always sees fresh state.json / clips
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Access-Control-Allow-Origin", "*")
        if "Accept-Ranges" not in self._headers_buffer_str():
            self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def _headers_buffer_str(self) -> str:
        try:
            return b"".join(self._headers_buffer).decode("latin-1")
        except Exception:
            return ""

    # quieter, prefixed logging
    def log_message(self, fmt, *args):
        sys.stderr.write("[serve] %s - %s\n" % (self.address_string(), fmt % args))

    # serve .mp4 with the right type even on odd platforms
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".m4v": "video/x-m4v",
        ".json": "application/json",
        ".js": "application/javascript",
    }


def _inject_config_tag() -> None:
    """Ensure dashboard.html pulls in /__config__.js (so the REPLAY flag and
    state-url reach the page). Idempotent; NEVER-RAISE — a failure just means
    the page runs with its built-in defaults (live mode)."""
    html_path = os.path.join(ROOT, "dashboard.html")
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
        # Idempotency: only skip if the ACTUAL <script src="/__config__.js">
        # element is already present. A naive `"/__config__.js" in html` check
        # is wrong because dashboard.html mentions the filename in a comment,
        # which would always match and the real tag would never be injected.
        if re.search(r'<script[^>]+src=["\']/__config__\.js', html):
            return
        tag = '<script src="/__config__.js"></script>\n'
        if "</head>" in html:
            html = html.replace("</head>", tag + "</head>", 1)
        else:
            html = tag + html
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        print("[serve] injected /__config__.js tag into dashboard.html")
    except Exception as exc:  # NEVER-RAISE
        print(f"[serve] WARN could not inject config tag "
              f"({type(exc).__name__}: {exc}) — page falls back to live mode")


def main() -> int:
    _inject_config_tag()
    state_url = _state_url()
    mode = "REPLAY (cached, zero API calls)" if REPLAY else "LIVE"
    print(f"[serve] root      = {ROOT}")
    print(f"[serve] mode      = {mode}")
    print(f"[serve] state url = {state_url}")
    if REPLAY:
        clips = _list_cached_clips()
        print(f"[serve] clips     = {len(clips)} cached "
              f"({', '.join(clips) if clips else 'NONE — replay video will be blank!'})")
        print("[serve] replay state.json is synthesized with local clip refs only "
              "(no expired https)")
    print(f"[serve] open      http://{HOST}:{PORT}/dashboard.html")
    try:
        httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    except Exception as exc:  # NEVER-RAISE (port in use, etc.)
        print(f"[serve] FATAL could not bind {HOST}:{PORT}: "
              f"{type(exc).__name__}: {exc}")
        return 1
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[serve] shutting down")
    except Exception as exc:  # NEVER-RAISE
        print(f"[serve] server loop error: {type(exc).__name__}: {exc}")
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
