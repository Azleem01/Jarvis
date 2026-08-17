"""Resolve a spoken contact name to a real, saved contact.

Local speech-to-text spells the same person differently every time ("mom" vs
"mum"), and people ask for contacts by relationship ("text daddy") rather than
by the name the contact is actually saved under. Matching the literal string
grabs the wrong person — the single thing the user most wants to avoid.

``resolve_contact`` layers three ideas:
  1. **Relationship/spelling normalisation** — mom/mum/mommy/mother collapse to
     one group, so a contact saved as "Mum" is found when the user says "mom".
  2. **Fuzzy matching** (stdlib ``difflib``, no new dependency) against the real
     contact pool: ``WHATSAPP_CONTACTS`` from .env, plus every name and taught
     alias in the learnt cache (``tools/whatsapp.py``).
  3. **Confirm when unsure** — a clear single winner is returned as a match; two
     plausible people come back as "ambiguous" so the caller can ask rather than
     guess.

NOTE: no ``from __future__ import annotations`` — string annotations break
google-genai's automatic function calling (see tools/os_tools.py).
"""

import difflib

import config

# Spoken variants that all refer to the same person. Normalising to the whole
# group BEFORE matching is what lets "mom" find a contact saved as "Mum", and
# "daddy" find one saved as "Dad".
_RELATIONSHIP_GROUPS = [
    {"mum", "mom", "mummy", "mommy", "mother", "mama", "ma"},
    {"dad", "daddy", "father", "papa", "pa", "pops"},
    {"grandma", "grandmother", "granny", "nan", "nana", "gran"},
    {"grandpa", "grandfather", "granddad", "grandad", "gramps"},
    {"bro", "brother"},
    {"sis", "sister"},
    {"hubby", "husband"},
    {"wifey", "wife"},
]


def _variants(name):
    """Lowercased forms to try for ``name``.

    Always includes the cleaned name itself; if it is a relationship word, adds
    every other word in its group so a contact saved under any of them matches.
    """
    base = " ".join(str(name or "").strip().lower().split())
    forms = {base} if base else set()
    for group in _RELATIONSHIP_GROUPS:
        if base in group:
            forms |= group
    return forms


def _pool():
    """Search key (lowercased) -> canonical display name for every known contact.

    Draws from .env ``WHATSAPP_CONTACTS`` (keys are the spoken names) and the
    learnt cache (each entry's ``name`` plus any taught ``aliases``).
    """
    pool = {}
    for key in config.WHATSAPP_CONTACTS:
        k = str(key).strip().lower()
        if k:
            pool[k] = str(key).strip()
    try:
        from tools import whatsapp

        cache = whatsapp._load_cache()
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[contacts] cache unavailable: {exc}")
        cache = {}
    for key, entry in cache.items():
        if isinstance(entry, dict):
            name = str(entry.get("name") or key).strip()
            if name:
                pool[name.lower()] = name
            for alias in entry.get("aliases", []) or []:
                a = str(alias).strip().lower()
                if a:
                    pool[a] = name
        else:
            k = str(key).strip().lower()
            if k:
                pool[k] = str(key).strip()
    return pool


def resolve_contact(spoken):
    """Map a spoken name to a real contact.

    Returns one of:
      ``("match", canonical_name)`` — a confident single contact
      ``("ambiguous", [names])``    — several plausible; the caller should ask
      ``("none", [])``              — nothing close; the caller may search the UI
    """
    forms = _variants(spoken)
    if not forms:
        return ("none", [])
    pool = _pool()
    if not pool:
        return ("none", [])

    # 1. Exact, after variant normalisation.
    for form in forms:
        if form in pool:
            return ("match", pool[form])

    # 2. Fuzzy. Score every close key, keeping the best score per real contact.
    keys = list(pool)
    best = {}
    for form in forms:
        for key in difflib.get_close_matches(
            form, keys, n=5, cutoff=config.CONTACT_MATCH_THRESHOLD
        ):
            score = difflib.SequenceMatcher(None, form, key).ratio()
            name = pool[key]
            best[name] = max(best.get(name, 0.0), score)
    if not best:
        return ("none", [])

    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    if len(ranked) == 1:
        return ("match", ranked[0][0])
    # A clear winner beats the runner-up by the margin; otherwise ask.
    if ranked[0][1] - ranked[1][1] >= config.CONTACT_MATCH_MARGIN:
        return ("match", ranked[0][0])
    return ("ambiguous", [name for name, _ in ranked[:3]])
