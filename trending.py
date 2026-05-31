#!/usr/bin/env python3
"""
trending.py — Module D (tiny): live "what's trending" signal for the loop.

The loop seeds its prompts with a short list of trend strings. By default those
come from contracts.TRENDING (a hand-curated, always-available cache). This
module OPTIONALLY upgrades that to a LIVE pull from the YouTube Data API
(mostPopular Music videos in the US) so the demo can ride real, current trends —
but it is built to be DEMO-SAFE: it never raises and never blocks beyond ~2s.

    get_trending(vertical: str = "dance") -> list[str]   # 8-10 trend strings

PRIMARY  : YouTube Data API v3 videos.list (chart=mostPopular, category=Music).
FALLBACK : contracts.TRENDING — used whenever YOUTUBE_API_KEY is missing, the
           request errors / rate-limits / times out, or yields nothing.

Prints exactly ONE source line so the loop's logs make the path obvious:
    [trending] source=youtube  (count=N)
    [trending] source=cached    (count=N)

    $ python trending.py     # prints the resolved trend list (cached w/o a key)
"""
from __future__ import annotations

import os

import contracts


# ---------------------------------------------------------------------------
# Project env loader — same KEY=VALUE pattern judge.py uses, so YOUTUBE_API_KEY
# (and any other key) defined in .modal.env / .env is visible to os.environ
# without requiring it to be exported in the shell. Real shell env always wins
# (setdefault never overwrites). Never raises; missing files are fine.
# ---------------------------------------------------------------------------
def _load_project_env(filename: str) -> None:
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
        pass  # no env file -> rely on real environment / cached fallback
    except Exception:
        pass  # NEVER-RAISE: a malformed env file must not break the loop


for _fn in (".modal.env", ".env"):
    _load_project_env(_fn)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_YT_ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"
_REQUEST_TIMEOUT = 2.0           # seconds — HARD cap so we never block the demo
_MAX_RESULTS = 20                # how many popular videos to pull
_MIN_TRENDS = 8                  # contract: return 8-10
_MAX_TRENDS = 10

# Light dance-vertical keyword filter (case-insensitive substring match).
_DANCE_KEYWORDS = ("dance", "challenge", "choreography", "choreo", "remix", "#")


def _cached() -> list[str]:
    """Always-available fallback: a COPY of the hand-curated trend cache."""
    print(f"[trending] source=cached    (count={len(contracts.TRENDING)})")
    return list(contracts.TRENDING)


def _select_titles(titles: list[str]) -> list[str]:
    """Pick 8-10 trend strings, PREFERRING ones with dance-vertical keywords.

    Keeps original order, de-dupes, and tops up with the remaining titles if the
    keyword-matched set is under _MIN_TRENDS. Returns [] only if `titles` is [].
    """
    seen: set[str] = set()
    preferred: list[str] = []
    rest: list[str] = []
    for t in titles:
        t = (t or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        low = t.lower()
        if any(kw in low for kw in _DANCE_KEYWORDS):
            preferred.append(t)
        else:
            rest.append(t)

    chosen = preferred[:]
    if len(chosen) < _MIN_TRENDS:
        chosen.extend(rest[: _MIN_TRENDS - len(chosen)])
    return chosen[:_MAX_TRENDS]


def get_trending(vertical: str = "dance") -> list[str]:
    """Return 8-10 trend strings, LIVE from YouTube if possible else cached.

    NEVER raises and NEVER blocks beyond ~2s: any missing key, network/HTTP
    error, rate-limit, timeout, bad JSON, or empty result degrades instantly to
    contracts.TRENDING. The `vertical` arg currently tunes only the keyword
    filter intent (dance); the API pull is the US "Music" most-popular chart.
    """
    try:
        api_key = (os.environ.get("YOUTUBE_API_KEY") or "").strip()
        if not api_key:
            return _cached()

        # requests is already a dependency (judge.py uses it). Guard the import
        # anyway so a bare machine still degrades to cached instead of crashing.
        try:
            import requests
        except Exception:
            return _cached()

        params = {
            "part": "snippet",
            "chart": "mostPopular",
            "regionCode": "US",
            "videoCategoryId": "10",      # 10 = Music
            "maxResults": _MAX_RESULTS,
            "key": api_key,
        }
        resp = requests.get(_YT_ENDPOINT, params=params, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        items = data.get("items") or []
        titles = [
            it["snippet"]["title"]
            for it in items
            if isinstance(it, dict) and isinstance(it.get("snippet"), dict)
            and it["snippet"].get("title")
        ]

        trends = _select_titles(titles)
        if not trends:
            return _cached()

        print(f"[trending] source=youtube  (count={len(trends)})")
        return trends
    except Exception as e:  # NEVER-RAISE: any failure -> cached
        print(f"[trending] youtube pull failed ({type(e).__name__}: {e}) -> cached")
        return _cached()


if __name__ == "__main__":
    _trends = get_trending()
    print(f"\nresolved {len(_trends)} trend(s):")
    for _i, _t in enumerate(_trends):
        print(f"  {_i + 1:>2}. {_t}")
