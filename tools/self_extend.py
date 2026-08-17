"""Azleem teaching itself a new tool: write it, verify it, install, restart.

When a request has no matching tool and the user wants that capability added for
good, ``add_capability`` writes a fresh tool module into ``tools/generated/``
(the isolated home for self-written code), proves it is safe to load, and
restarts Azleem so the new tool goes live. The original request is carried
across the restart and run automatically.

Three deliberate safety properties:

* **Confirmed, not autonomous.** Like ``power_action``, it is a two-step: the
  first call *arms* — it writes the candidate tool and describes it — and only a
  second call with ``confirm=True`` (after the user actually says "confirm")
  installs and restarts. A single mis-transcription can't rewrite the codebase.
* **Verified before it goes live.** On confirm the module is checked in a
  subprocess — it must import, expose a callable ``TOOL`` with a ROUTING line,
  and build cleanly into a Gemini function declaration — and the offline prompt
  suite is run. Any failure rolls the file back and refuses to restart.
* **Isolated.** Everything self-written lands in ``tools/generated/`` and is
  loaded there behind a guard, so a bad tool can never break the hand-written
  core or stop Azleem starting.

It writes the *tool*; it cannot conjure *credentials*. A generated email or API
tool reads its secrets from environment variables and reports the missing one
honestly when they are absent — this feature grows Azleem's toolset, it does not
grant access nothing configured it with.

NOTE: no ``from __future__ import annotations`` here — string annotations break
google-genai's automatic function calling (see tools/os_tools.py).
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

import cancellation
import config
import gemini_client
import self_restart

# Project root (where main.py and tests/ live) — the cwd for safety-check
# subprocesses. The generated package sits one level down, under tools/.
_ROOT = Path(__file__).resolve().parent.parent
_GENERATED_DIR = Path(__file__).resolve().parent / "generated"

_TOOL_PROMPT = (
    "You are extending a Windows voice assistant by writing ONE new tool it "
    "will load into itself as a Python module. The capability to add:\n{task}\n\n"
    "Write a complete, self-contained Python 3 module. Follow this convention "
    "EXACTLY:\n"
    "- Define exactly one function: def <snake_case_name>(<typed args>) -> str. "
    "Every parameter MUST have a simple type hint (str, int, float or bool) and "
    "sensible defaults where natural. The function returns a short "
    "human-readable status string and NEVER raises: catch its own errors and "
    "return a message describing what went wrong.\n"
    "- Give it a clear Google-style docstring — one line on what it does, then "
    "an Args: section. The assistant's router reads this docstring to decide "
    "when to call the tool, so be specific and concrete.\n"
    "- After the function, add two module-level lines: TOOL = <the_function> "
    "and ROUTING = \"- <snake_case_name>: <one line on when to use it>.\"\n"
    "- You MAY use the standard library and commonly installed packages "
    "(requests, pathlib, os, subprocess, smtplib, and so on). Read any secret "
    "(API key, password, token, address) from an environment variable via "
    "os.environ, and if it is missing return a message naming the exact "
    "variable to set. NEVER hard-code a credential.\n"
    "- Do NOT write 'from __future__ import annotations'. No input() calls, no "
    "GUI windows, no infinite loops.\n"
    "Respond with ONLY the Python code — no markdown fences, no commentary."
)

# Staged candidate, in-process only: arm and confirm happen in the same process
# lifetime (the restart is what ends it), so no disk persistence is needed.
# Shape: {"name", "path", "task", "desc"}.
_staged = None


def add_capability(task: str, confirm: bool = False) -> str:
    """Permanently teach Azleem a NEW tool when no existing tool can do a task.

    Use this only when the user wants a genuinely missing capability added for
    good ("learn to ...", "you should be able to ...", or a request nothing in
    the toolset covers) — not for a one-off, which is accomplish_with_code. It
    writes a new tool into Azleem's own code and restarts to load it, so it is
    confirmed out loud like a power action: call it WITHOUT confirm first to
    write the candidate tool and describe it, and only pass confirm=true once the
    user has actually said "confirm". Installing restarts Azleem and then runs
    the original request with the new tool.

    Args:
        task: A full natural-language description of the capability to add.
        confirm: True only when the user is confirming a tool that was just
            described to them. Never set this on a first request.

    Returns:
        A status string: the confirmation to speak, or the install outcome.
    """
    global _staged

    if not config.SELF_EXTEND_ENABLED:
        return (
            "Teaching myself new tools is switched off "
            "(set SELF_EXTEND_ENABLED=true in .env to allow it)."
        )

    if confirm:
        return _install()

    code = _generate(_TOOL_PROMPT.format(task=task))
    if code is None:
        return "I couldn't write a tool for that right now — try rephrasing it."
    if cancellation.cancelled():
        return "Aborted — you cancelled."
    if _has_future_annotations(code):
        return "I wrote an unsafe tool for that; I won't install it."

    parsed = _parse(code)
    if parsed is None:
        return (
            "I couldn't turn that into a valid tool — say it a different way and "
            "I'll try again."
        )
    name, desc = parsed

    try:
        _GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        path = _GENERATED_DIR / f"{name}.py"
        path.write_text(code + "\n", encoding="utf-8")
    except OSError as exc:
        return f"I couldn't save the new tool: {exc}"

    _staged = {"name": name, "path": path, "task": task, "desc": desc}
    return (
        f"I can add a `{name}` tool: {desc[:90]} It writes into my own code and "
        f"I'll restart to load it. Say 'confirm' to go ahead, or 'cancel'."
    )


def _install() -> str:
    """Verify the staged tool and, if it passes, restart to load it."""
    global _staged
    staged = _staged
    _staged = None  # single use, cleared whatever the outcome
    if staged is None:
        return "Nothing is waiting to be installed. Ask me to add the capability first."

    ok, detail = _validate(staged["name"])
    if not ok:
        _rollback(staged["path"])
        return f"The new tool didn't pass its safety checks, so I didn't install it: {detail}"

    # Carry the original request across the restart, then relaunch detached.
    self_restart.write_pending(staged["task"])
    if not self_restart.spawn_restart():
        _rollback(staged["path"])
        return "I built the tool but couldn't restart to load it, so I rolled it back."
    return (
        f"Installing `{staged['name']}` and restarting — "
        f"I'll run your request as soon as I'm back."
    )


def _rollback(path) -> None:
    """Remove a staged module so a failed install leaves no trace."""
    try:
        Path(path).unlink()
    except OSError:
        pass


def _validate(name: str):
    """Prove the module loads, declares cleanly, and the suite still passes.

    Returns (ok, detail). The subprocess import is deliberately NOT the guarded
    package loader (which would swallow a broken module and report success) — it
    imports the specific module directly so a syntax error surfaces here.
    """
    ok, detail = _run_python(["-c", _MODULE_CHECK.format(name=name)])
    if not ok:
        return False, detail
    if "CHECK-OK" not in detail:
        return False, "the tool did not report itself as valid"
    ok, detail = _run_python(["-m", "unittest", "tests.test_prompts"])
    if not ok:
        return False, "the prompt/schema tests failed with the new tool"
    return True, ""


_MODULE_CHECK = (
    "import importlib\n"
    "m = importlib.import_module('tools.generated.{name}')\n"
    "fn = getattr(m, 'TOOL', None)\n"
    "assert callable(fn), 'the module has no callable TOOL'\n"
    "r = getattr(m, 'ROUTING', '')\n"
    "assert isinstance(r, str) and r.strip(), 'the module has no ROUTING line'\n"
    "from google.genai import Client, types\n"
    "c = Client(api_key='schema-only-not-a-real-key')._api_client\n"
    "types.FunctionDeclaration.from_callable(callable=fn, client=c)\n"
    "print('CHECK-OK')\n"
)


def _run_python(args):
    """Run the interpreter from the project root; return (ok, combined_output)."""
    try:
        done = subprocess.run(
            [sys.executable, *args],
            capture_output=True, text=True, cwd=str(_ROOT),
            timeout=config.SELF_EXTEND_TEST_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return False, "the safety checks timed out"
    except OSError as exc:
        return False, f"the safety checks couldn't run ({exc})"
    output = ((done.stdout or "") + (done.stderr or "")).strip()
    ok = done.returncode == 0
    # On failure, surface the last meaningful line rather than a wall of text.
    if not ok:
        lines = [ln for ln in output.splitlines() if ln.strip()]
        output = lines[-1][:160] if lines else "the checks failed"
    return ok, output


def _generate(prompt: str):
    try:
        response = gemini_client.generate_content(contents=prompt)
    except Exception:
        return None
    code = (response.text or "").strip()
    code = re.sub(r"^```[a-zA-Z]*\n?", "", code)
    code = re.sub(r"\n?```$", "", code).strip()
    return code or None


def _has_future_annotations(code: str) -> bool:
    for line in code.splitlines():
        stripped = line.strip()
        if stripped.startswith("from __future__ import") and "annotations" in stripped:
            return True
    return False


def _parse(code: str):
    """Return (tool_name, one_line_description) if the module is well-formed.

    Requires a valid ``TOOL = <name>`` bound to a top-level function, a
    ``ROUTING`` assignment, and a snake_case identifier — the same shape the
    package loader and the SDK expect.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None

    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    tool_name = None
    has_routing = False
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id == "TOOL" and isinstance(node.value, ast.Name):
                tool_name = node.value.id
            elif target.id == "ROUTING":
                has_routing = True

    if tool_name is None and len(funcs) == 1:
        tool_name = next(iter(funcs))  # unambiguous single function
    if not has_routing:
        return None
    if tool_name is None or tool_name not in funcs:
        return None
    if not re.fullmatch(r"[a-z][a-z0-9_]*", tool_name):
        return None

    doc = (ast.get_docstring(funcs[tool_name]) or "").strip()
    desc = doc.splitlines()[0] if doc else f"a new {tool_name} tool."
    return tool_name, desc[:120]


def _reset_for_tests() -> None:
    """Clear the staged slot. Tests only — a stage must not leak between cases."""
    global _staged
    _staged = None
