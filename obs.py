#!/usr/bin/env python3
"""
obs.py — Raindrop Workshop observability helper (BACKEND).

This is the ONLY module that touches the Raindrop SDK. Everything is ADDITIVE
and NEVER-RAISE: a dead/absent SDK, a wrong API, or a flaky Workshop can never
break the control loop. The state.json mirror (`mirror()`) is a completely
independent channel that does NOT depend on the SDK — so the demo banner is
bulletproof even when Raindrop is off or broken.

Two channels:
  1. SDK channel (best-effort) — begin_run / span / finish_run / the SDK half of
     mirror(). Every call is wrapped so ALL exceptions are swallowed.
  2. state.json mirror (bulletproof) — mirror() always appends a RaindropEvent to
     state["raindrop"]["events"] (see contracts.RaindropEvent / Raindrop), even
     when the SDK is dead. This is what the dashboard reads.

Feature gate:
  USE_RAINDROP=1 (default) -> both channels active.
  USE_RAINDROP=0           -> feature FULLY off: no SDK import, mirror() is a
                              no-op, and loop.py never adds a "raindrop" key.
"""
from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Feature gate + constants
# ---------------------------------------------------------------------------
USE_RAINDROP: bool = os.environ.get("USE_RAINDROP", "1") != "0"
WORKSHOP_URL: str = "http://localhost:5899"

# ---------------------------------------------------------------------------
# SDK import + init (best-effort; the mirror NEVER depends on this succeeding)
# ---------------------------------------------------------------------------
_rd = None  # the raindrop.analytics module, or None when off / unavailable

if USE_RAINDROP:
    # Point the SDK at the local Workshop debugger unless the caller already set
    # one. setdefault never clobbers an explicit RAINDROP_LOCAL_DEBUGGER.
    os.environ.setdefault("RAINDROP_LOCAL_DEBUGGER", "http://localhost:5899/v1/")
    try:
        import raindrop.analytics as _rd  # type: ignore
        _rd.init(os.environ.get("RAINDROP_WRITE_KEY") or "local-workshop")
    except Exception as e:  # SDK missing / init failed: keep the mirror alive
        _rd = None
        print(f"[raindrop] SDK off ({e}); state.json mirror still active")

# Module-level handle to the current run's Interaction (or None).
_interaction = None


# ---------------------------------------------------------------------------
# Run lifecycle — begin_run / finish_run
# ---------------------------------------------------------------------------
def begin_run(event: str, user_id: str, input_str: str):
    """Open one Raindrop Interaction for the whole run. Best-effort.

    Returns the Interaction on success, else None. Never raises. `user_id` is
    REQUIRED by raindrop.begin (confirmed API). Stores the Interaction at module
    level so span()/mirror()/finish_run() can reference it.
    """
    global _interaction
    if not (USE_RAINDROP and _rd):
        return None
    try:
        _interaction = _rd.begin(user_id=user_id, event=event, input=input_str)
        return _interaction
    except Exception:
        _interaction = None
        return None


def finish_run(output: str) -> None:
    """Close the current Interaction and flush the SDK buffer. Never raises.

    Safe on every exit path (normal, early-stop, exception). No-op when the SDK
    is off or no Interaction was opened.
    """
    global _interaction
    if _interaction is not None:
        try:
            _interaction.finish(output=output)
        except Exception:
            pass
    if _rd is not None:
        try:
            _rd.flush()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Per-iteration span — span(name, attrs)
# ---------------------------------------------------------------------------
def span(name: str, attrs: dict | None = None) -> None:
    """Best-effort SDK span for one iteration (a plain call, NOT a context
    manager the caller must wrap). Records `attrs` as span properties.

    No-op-safe: returns immediately if the SDK/Interaction are off, and swallows
    ALL exceptions. Tries the documented helpers in order:
      _rd.task_span(name) context manager + _rd.set_span_properties(attrs)
    falling back to _interaction.start_span(...) + ManualSpan.set_properties.
    """
    if not (USE_RAINDROP and _rd and _interaction):
        return
    props = dict(attrs or {})
    # Preferred: task_span context manager (auto opens/closes the span) and
    # attach properties to the currently-active span.
    try:
        cm = _rd.task_span(name)
        with cm:
            try:
                _rd.set_span_properties(props)
            except Exception:
                pass
        return
    except Exception:
        pass
    # Fallback: ManualSpan via the Interaction (explicit end()).
    try:
        ms = _interaction.start_span("task", name)
        try:
            ms.set_properties(props)
        except Exception:
            pass
        try:
            ms.end()
        except Exception:
            pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# THE BULLETPROOF CHANNEL — mirror(...)
# ---------------------------------------------------------------------------
def mirror(state: dict, iter_n: int, type_: str, label: str, detail: str,
           severity: str = "info") -> None:
    """Append one RaindropEvent to state["raindrop"]["events"] AND best-effort
    emit it to the SDK. NEVER raises.

    The state.json append ALWAYS happens (even when the SDK is dead) — this is
    the channel the dashboard reads. When USE_RAINDROP=0 the feature is fully
    off and this is a complete no-op (no "raindrop" key is ever added).

    Args mirror contracts.RaindropEvent:
      iter_n   -> event["iter"]
      type_    -> "trace" | "alarm"
      label    -> short label
      detail   -> one-line human detail
      severity -> "info" | "warn"
    """
    if not USE_RAINDROP:
        return  # feature fully off — never touch state

    # --- bulletproof: always append to state, independent of the SDK ---
    try:
        rd = state.setdefault("raindrop", {"workshop_url": WORKSHOP_URL, "events": []})
        rd.setdefault("workshop_url", WORKSHOP_URL)
        rd.setdefault("events", []).append({
            "iter": iter_n,
            "type": type_,
            "label": label,
            "detail": detail,
            "severity": severity,
        })
    except Exception:
        # Even the mirror append must never raise into the loop. If `state` is
        # somehow not a dict, silently drop — the loop keeps running.
        pass

    # --- best-effort SDK signal (swallow everything) ---
    if _rd is not None and _interaction is not None:
        try:
            _rd.track_signal(
                event_id=_interaction.id,
                name=f"{type_}:{label}",
                signal_type="default",
                properties={
                    "iter": iter_n,
                    "type": type_,
                    "label": label,
                    "detail": detail,
                    "severity": severity,
                },
            )
        except Exception:
            # Fall back to a span if track_signal is unavailable/changed.
            try:
                span(label, {"iter": iter_n, "type": type_,
                             "detail": detail, "severity": severity})
            except Exception:
                pass
