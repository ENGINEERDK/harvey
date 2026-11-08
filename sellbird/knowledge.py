"""Your own knowledge, matched to each prospect.

Two optional markdown files the operator keeps in skills/:

- ``industry_playbook.md``: per industry, where material moves and where the
  product fits. Lets an email be specific about their kind of plant before
  there's any case study.
- ``case_studies.md``: the operator's real deployments. The knowledge bank
  grows as you deploy; the writer only uses an entry that matches.

Both use the same shape: a ``## Heading`` per entry, and a ``match:`` line of
comma-separated words or phrases. An entry is chosen when one of its match
phrases appears in what we know about the prospect (their industry, title,
company and brief). Entries without a match line are never chosen, so a
half-written entry can't leak into every email.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PLAYBOOK = "industry_playbook"
CASE_STUDIES = "case_studies"
MAX_ENTRIES = 2
MAX_ENTRY_CHARS = 1500


@dataclass(frozen=True)
class Entry:
    heading: str
    match: tuple[str, ...]
    body: str          # the whole entry, heading included


def parse_entries(text: str) -> list[Entry]:
    entries = []
    for chunk in re.split(r"(?m)^(?=## )", text or ""):
        if not chunk.startswith("## "):
            continue
        heading = chunk.splitlines()[0][3:].strip()
        m = re.search(r"(?im)^\s*(?:[-*]\s*)?match\s*:\s*(.+)$", chunk)
        phrases = tuple(p.strip().lower() for p in (m.group(1).split(",") if m else [])
                        if len(p.strip()) >= 3)
        if heading and phrases:
            entries.append(Entry(heading, phrases, chunk.strip()[:MAX_ENTRY_CHARS]))
    return entries


def _words(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()) + " "


def matching(entries: list[Entry], about: str, limit: int = MAX_ENTRIES) -> list[Entry]:
    """Entries whose match phrases appear in ``about``, most matches first."""
    hay = _words(about)
    scored = []
    for i, entry in enumerate(entries):
        hits = sum(1 for p in entry.match if _words(p).strip() and _words(p) in hay)
        if hits:
            scored.append((-hits, i, entry))
    return [e for _, _, e in sorted(scored)[:limit]]


def knowledge_block(playbook: str, case_studies: str, about: str) -> str:
    """The prompt section for one prospect ("" when nothing matches)."""
    parts = []
    play = matching(parse_entries(playbook), about)
    if play:
        parts.append(
            "INDUSTRY PLAYBOOK (the operator's notes on plants like theirs; use it to\n"
            "say where the product fits. It describes their kind of plant, not\n"
            "this company: say \"in plants like yours\" or \"often\", never claim it\n"
            "as a fact about them):\n\n" + "\n\n".join(e.body for e in play))
    cases = matching(parse_entries(case_studies), about, limit=1)
    if cases:
        parts.append(
            "OUR DEPLOYMENTS (real, written by the operator). You may mention at most\n"
            "one, exactly as written: never add numbers, results or details it\n"
            "doesn't give, and never name the customer unless it says\n"
            "\"can_name_customer: yes\" (otherwise describe them as it does):\n\n"
            + "\n\n".join(e.body for e in cases))
    return ("\n\n" + "\n\n".join(parts)) if parts else ""
