"""Solve tasks by writing and running Python, then share the solution.

``solve_with_python`` asks Gemini for a self-contained script, runs it locally
with a timeout, repairs it once if it crashes, and shares the result: as a
secret GitHub Gist link when GITHUB_TOKEN is configured, otherwise by opening
the local solution folder.

Safety: this executes model-generated code on the user's machine, at the
user's own spoken request. It is bounded by a 30-second timeout, runs inside
its own solution folder, and both the code and its output are saved there for
inspection. Do not point it at tasks that need elevated privileges.

NOTE: no ``from __future__ import annotations`` — string annotations break
google-genai's automatic function calling (see tools/os_tools.py).
"""

import datetime as _dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx

import cancellation
import config
import gemini_client

_TIMEOUT_S = 30
_CODE_PROMPT = (
    "Write a complete, self-contained Python 3 script that accomplishes this "
    "task:\n{task}\n\n"
    "Requirements: standard library only if possible; print the results to "
    "stdout clearly; no user input (no input() calls); no GUI. "
    "Respond with ONLY the Python code — no markdown fences, no commentary."
)
_REPAIR_PROMPT = (
    "This Python script failed. Fix it and respond with ONLY the corrected "
    "code — no markdown fences, no commentary.\n\n"
    "TASK:\n{task}\n\nSCRIPT:\n{code}\n\nERROR OUTPUT:\n{error}"
)


def solve_with_python(task: str) -> str:
    """Solve a task by writing and running a Python script, then share it.

    Use for anything computational or programmatic: calculations, data
    generation, text processing, algorithm questions ("compute the first 100
    primes", "generate a strong password", "convert this list to CSV"). The
    script and its output are saved, and shared as a link when possible.

    Args:
        task: Natural-language description of what the script must do.

    Returns:
        A status string with the outcome and a shareable link or local path.
    """
    code = _generate(_CODE_PROMPT.format(task=task))
    if code is None:
        return "Gemini couldn't produce a script for that right now."
    if cancellation.cancelled():
        return "Aborted — you pressed Esc."

    folder = _solution_folder(task)
    script = folder / "solution.py"
    script.write_text(code + "\n", encoding="utf-8")

    ok, output = _run(script)
    if not ok and not cancellation.cancelled():
        fixed = _generate(_REPAIR_PROMPT.format(task=task, code=code, error=output))
        if fixed:
            script.write_text(fixed + "\n", encoding="utf-8")
            ok, output = _run(script)

    (folder / "output.txt").write_text(output, encoding="utf-8")
    if cancellation.cancelled():
        return "Aborted — you pressed Esc."

    status = "Solved" if ok else "Script written but it still errors"
    excerpt = output.strip().splitlines()
    excerpt = " / ".join(excerpt[:3])[:160]

    notebook = _build_notebook(task, script.read_text(encoding="utf-8"), output)
    (folder / "solution.ipynb").write_text(notebook, encoding="utf-8")

    links = _share_gist(task, script, output, notebook) if config.GITHUB_TOKEN else None
    if links:
        colab, gist = links
        if colab:
            return (
                f"{status}. Output: {excerpt} — solution link (opens in Google "
                f"Colab, viewable by anyone): {colab} (code: {gist})"
            )
        return f"{status}. Output: {excerpt} — solution link: {gist}"
    try:
        os.startfile(str(folder))  # type: ignore[attr-defined]
    except OSError:
        pass
    hint = "" if config.GITHUB_TOKEN else \
        " (Add GITHUB_TOKEN to .env to get shareable links.)"
    return f"{status}. Output: {excerpt} — saved in {folder}.{hint}"


def _generate(prompt: str):
    try:
        response = gemini_client.generate_content(contents=prompt)
    except Exception:
        return None
    code = (response.text or "").strip()
    # Strip markdown fences if the model added them anyway.
    code = re.sub(r"^```[a-zA-Z]*\n?", "", code)
    code = re.sub(r"\n?```$", "", code).strip()
    return code or None


def _solution_folder(task: str) -> Path:
    slug = re.sub(r"[^\w\- ]", "", task.lower())
    slug = re.sub(r"\s+", "-", slug.strip())[:40] or "task"
    folder = (
        Path.home() / "Documents" / "Azleem" / "solutions"
        / f"{slug}-{_dt.datetime.now():%Y%m%d_%H%M%S}"
    )
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _run(script: Path) -> "tuple[bool, str]":
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, timeout=_TIMEOUT_S,
            cwd=str(script.parent),
        )
    except subprocess.TimeoutExpired:
        return False, f"(script exceeded the {_TIMEOUT_S}s time limit)"
    output = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
    return result.returncode == 0, output.strip() or "(no output)"


def _build_notebook(task: str, code: str, output: str) -> str:
    """A minimal nbformat-4 notebook: task, code cell, and its stdout.

    The stdout is attached as a stream output so the notebook shows results
    without needing execution; graders can still hit Run All in Colab (which
    also renders any plots the code produces).
    """
    nb = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {"name": "python3", "display_name": "Python 3"},
            "language_info": {"name": "python"},
        },
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": f"# Solution\n\n**Task:** {task}",
            },
            {
                "cell_type": "code",
                "metadata": {},
                "execution_count": 1,
                "source": code,
                "outputs": [
                    {
                        "output_type": "stream",
                        "name": "stdout",
                        "text": output or "(no output)",
                    }
                ],
            },
        ],
    }
    return json.dumps(nb, indent=1)


def _share_gist(task: str, script: Path, output: str, notebook: str):
    """Upload solution + output + notebook as a secret gist.

    Returns:
        (colab_url_or_None, gist_url) on success, or None on any failure.
        The Colab URL (colab.research.google.com/gist/...) opens the notebook
        directly in Google Colab for anyone with the link — which is what
        course platforms asking for a "shared Colab notebook" accept.
    """
    try:
        resp = httpx.post(
            "https://api.github.com/gists",
            headers={
                "Authorization": f"Bearer {config.GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
            json={
                "description": f"Azleem solution: {task[:80]}",
                "public": False,
                "files": {
                    "solution.py": {"content": script.read_text(encoding="utf-8")},
                    "output.txt": {"content": output or "(no output)"},
                    "solution.ipynb": {"content": notebook},
                },
            },
            timeout=20,
        )
        if resp.status_code == 201:
            data = resp.json()
            gist_url = data.get("html_url")
            gist_id = data.get("id")
            owner = (data.get("owner") or {}).get("login")
            colab = (
                f"https://colab.research.google.com/gist/{owner}/{gist_id}/solution.ipynb"
                if owner and gist_id else None
            )
            return colab, gist_url
        print(f"[coding] gist upload failed: HTTP {resp.status_code}")
    except Exception as exc:
        print(f"[coding] gist upload failed: {exc}")
    return None
