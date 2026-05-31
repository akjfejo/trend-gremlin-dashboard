---
name: running-modal-apps
description: Use when running, deploying, authenticating, or debugging Modal in this repo — e.g. `modal run judge.py`, `python loop.py` with USE_MOCK_JUDGE=0, "modal setup", token/profile/"App is not running" errors, or adding a Modal Secret. Covers Modal 1.x basics and the content-judge app.
---

# Running Modal Apps

## Overview
Modal runs Python functions on remote workers: define a `modal.App`, decorate functions, then `modal run` a file or call `.remote()` / `.map()` from a `@app.local_entrypoint`. This repo's judge (`judge.py`, app **`content-judge`**) is Modal-backed.

## This repo is ALREADY authenticated — do NOT re-run `modal setup`
Credentials are wired project-wide:
- **CLI** (`modal run …`) → activated profile **`hackathon-modal`** in `~/.modal.toml`.
- **Python** (`python loop.py` / `python judge.py`) → `judge.py:_load_project_env()` auto-loads the gitignored **`.modal.env`** before `import modal`. A real `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` already in the shell always wins. (`loop.py` triggers this by importing `judge`.)

`.modal.env` is `KEY=VALUE` lines (from modal.com → Settings → API Tokens):
```
MODAL_TOKEN_ID=ak-...
MODAL_TOKEN_SECRET=as-...
```
Verify auth: `modal profile current` → should print `hackathon-modal`.

First-time setup on a *fresh machine* only:
```
pip install modal
python3 -m modal setup     # opens browser, writes ~/.modal.toml
```

## Run the judge / loop
`USE_MOCK_JUDGE` picks the judge: **unset or `1`** = FREE local mock (element-overlap scoring, no cloud/key); **`0`** = real GPT-4o vision on Modal. `loop.py` also has `USE_MOCK` for the generator; bare `python loop.py` mocks both (fully local).
```
python judge.py                   # FREE mock: no cloud, no key — gibberish set scores low
modal run judge.py                # real GPT-4o vision on 3 demo sets (needs openai-secret)
python loop.py                    # default: generator + judge BOTH mocked, fully local
USE_MOCK_JUDGE=0 python loop.py   # real judge (judge.JUDGE), mocked generator
```

## Required Modal Secret (real vision path)
`judge_one` reads `OPENAI_API_KEY` from a Secret named **`openai-secret`**. Create once:
```
modal secret create openai-secret OPENAI_API_KEY=sk-...
```

## Minimal Modal app (canonical shape)
```python
import modal
app = modal.App("example-get-started")

@app.function()
def square(x):
    print("This code is running on a remote worker!")
    return x ** 2

@app.local_entrypoint()
def main():
    print("the square is", square.remote(42))
```
Save it, then: `modal run get_started.py`

## Modal 1.x API quick reference (verified on v1.4)
| Need | API |
|---|---|
| App | `modal.App("name")` |
| Image | `modal.Image.debian_slim().pip_install("openai>=1.0")` |
| Bundle a local sibling module into the image | `image.add_local_python_source("contracts")` |
| Function config | `@app.function(image=, secrets=, max_containers=, timeout=, retries=)` |
| Input concurrency | `@modal.concurrent(max_inputs=10, target_inputs=8)` — place directly above the function, BELOW `@app.function` |
| Retries | `modal.Retries(max_retries=2)` |
| Secret | `modal.Secret.from_name("openai-secret")` |
| Fan out | `fn.map(items, return_exceptions=True)` |
| One remote call | `fn.remote(arg)` |
| Drive from a plain `python` process (not `modal run`) | `with app.run(): fn.map(...)` |

## Common mistakes
- Running `python3 -m modal setup` here — unnecessary; creds are already wired. Usually the only missing piece is `openai-secret`.
- `.map()` / `.remote()` from a plain script raising **"App is not running"** → wrap in `with app.run():` (loop.py drives the judge this way).
- Putting `@modal.concurrent` ABOVE `@app.function` → it goes directly above the function.
- Container `ImportError` for a sibling module → add it via `image.add_local_python_source("<module>")`.
