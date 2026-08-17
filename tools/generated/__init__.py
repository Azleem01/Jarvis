"""Runtime home for tools Azleem wrote for itself.

Every capability that ``add_capability`` (tools/self_extend.py) teaches Azleem
lands here as a single self-contained module, and this package is the *only*
place self-written code lives. That isolation is the safety story: a broken
generated module is skipped, not fatal — it can never corrupt the hand-written
core (llm_agent.py, main.py) or stop Azleem from starting. Rollback is deleting
one file.

Each generated module follows a fixed convention:

    def some_capability(arg: str) -> str:
        \"\"\"One line the router reads to decide when to call this.\"\"\"
        ...
        return "what happened"

    TOOL = some_capability
    ROUTING = "- some_capability: when to use it."

At import time this package collects every module's ``TOOL`` into ``TOOLS`` and
concatenates the ``ROUTING`` lines into ``ROUTING``. ``llm_agent`` folds both
into the tool list and system prompt it hands Gemini per request. Loading still
happens once, at import — so a freshly written tool only goes live after a
restart, which is exactly what ``add_capability`` triggers.

NOTE: no ``from __future__ import annotations`` — and generated modules are
forbidden it too (guarded in tests/test_prompts.py), because string annotations
break google-genai's automatic function calling.
"""

import importlib
import pkgutil
from pathlib import Path

# Header on the routing block, kept here so the block is self-contained wherever
# it is spliced into the prompt.
_ROUTING_HEADER = (
    "\nTools you added for yourself (still prefer a built-in tool when one "
    "already fits):\n"
)


def _collect(pkg_dir, prefix, importer=importlib.import_module):
    """Import every sibling module and gather its TOOL and ROUTING.

    Split out from the module body so it can be tested with a fake ``importer``
    (feeding it good and deliberately-broken modules) without touching the real
    package. A module that fails to import, or lacks a callable ``TOOL``, is
    skipped with a log line — never allowed to take the whole package down.
    """
    tools = []
    routing = []
    for info in pkgutil.iter_modules([str(pkg_dir)]):
        name = info.name
        if name.startswith("_"):
            continue
        try:
            module = importer(f"{prefix}.{name}")
            fn = getattr(module, "TOOL", None)
            if not callable(fn):
                print(f"[generated] skipping {name!r}: no callable TOOL")
                continue
            tools.append(fn)
            line = getattr(module, "ROUTING", "")
            if isinstance(line, str) and line.strip():
                routing.append(line.strip())
        except Exception as exc:  # a bad module must not sink the rest
            print(f"[generated] skipping {name!r}: {exc}")
    block = (_ROUTING_HEADER + "\n".join(routing) + "\n") if routing else ""
    return tools, block


TOOLS, ROUTING = _collect(Path(__file__).resolve().parent, __name__)
