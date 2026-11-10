"""A short brief on each company, written from its own website.

The writer knows a company only by what it's told. A search phrase
("engineering company") makes every email read the same; what the company
says about itself (what it makes, how, where, how big) is what lets the first
line be about them. The brief is built once per company from up to three of
its own pages (the homepage, then about / products / manufacturing pages),
summarised by one model call, and kept: a company is re-read at most every
BRIEF_MAX_AGE_DAYS, and a site that couldn't be read is tried again after
BRIEF_RETRY_DAYS, so credits aren't spent on it every cycle.

Every field is something the site says. Nothing is inferred about fit here:
where the product could help is the writer's job, and the writer marks it as
a suggestion, never a fact about them.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sellbird.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("sellbird.company_brief")

BRIEF_MAX_AGE_DAYS = 180
BRIEF_RETRY_DAYS = 30
PAGE_CHARS = 3500          # of visible text per page sent to the model
MAX_PAGES = 3

# Pages where manufacturers describe what they make and how, in the order
# they're tried after the homepage. The first two that answer are used.
BRIEF_PATHS = (
    "/about", "/about-us", "/company", "/who-we-are", "/our-company",
    "/products", "/our-products", "/manufacturing", "/infrastructure",
    "/facilities", "/capabilities", "/industries", "/solutions",
)

FIELDS = ("name", "makes", "industries_served", "processes", "sites",
          "scale", "material_flow", "growth", "customers")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _age_days(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        return (_now() - datetime.fromisoformat(str(stamp))).total_seconds() / 86400
    except ValueError:
        return None


def parse_brief(raw: str | None) -> dict:
    """The stored brief as a dict ({} when there is none or it's unreadable)."""
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_fresh(brief: dict, stamp: str | None) -> bool:
    """True when the stored brief (or a recent failure) shouldn't be redone."""
    age = _age_days(stamp)
    if age is None:
        return False
    if brief.get("failed"):
        return age < BRIEF_RETRY_DAYS
    return age < BRIEF_MAX_AGE_DAYS


def clean_brief(data) -> dict:
    """Keep only the known fields, as short plain strings."""
    if not isinstance(data, dict):
        return {}
    out = {}
    for key in FIELDS:
        value = data.get(key)
        if isinstance(value, list):
            value = "; ".join(str(v).strip() for v in value if str(v).strip())
        value = re.sub(r"\s+", " ", str(value or "")).strip()
        if value and value.lower() not in ("unknown", "n/a", "none", "not stated", "-"):
            out[key] = value[:400]
    return out


def brief_facts(brief: dict) -> list[str]:
    """Fact lines for the writer's prompt (empty when there's no brief)."""
    if not brief or brief.get("failed"):
        return []
    labels = {
        "makes": "What they make (from their own site)",
        "industries_served": "Industries they serve",
        "processes": "Manufacturing processes they mention",
        "sites": "Plants / sites",
        "scale": "Scale",
        "material_flow": "What their site says about material handling, stores or logistics",
        "growth": "Expansion, new lines or capacity they mention",
        "customers": "Customers they name",
    }
    lines = [f"- {label}: {brief[key]}" for key, label in labels.items() if brief.get(key)]
    if lines and brief.get("sources"):
        lines.append(f"- Source pages: {brief['sources']}")
    return lines


async def gather_pages(domain: str, news_paths=(), max_news: int = 0) -> list[tuple[str, str]]:
    """Up to MAX_PAGES (url, visible text) of the company's own site, plus up
    to ``max_news`` of ``news_paths`` (where announcements live)."""
    from sellbird.collectors.profile import ProfileCollector, _text

    pages: list[tuple[str, str]] = []
    async with ProfileCollector(state=None) as prof:
        site, home = await prof._resolve_site(domain.lower().removeprefix("www.").strip("/"))
        if not home:
            return pages
        pages.append((site, _text(home)[:PAGE_CHARS]))
        for path in BRIEF_PATHS:
            if len(pages) >= MAX_PAGES:
                break
            html = await prof._get(f"{site}{path}")
            if html and len(html) > 500:
                text = _text(html)[:PAGE_CHARS]
                # A path that redirects to the homepage adds nothing.
                if text and text[:400] != pages[0][1][:400]:
                    pages.append((f"{site}{path}", text))
        news = 0
        for path in news_paths:
            if news >= max_news:
                break
            html = await prof._get(f"{site}{path}")
            if html and len(html) > 500:
                text = _text(html)[:PAGE_CHARS]
                if text and text[:400] != pages[0][1][:400]:
                    pages.append((f"{site}{path}", text))
                    news += 1
    return pages


def brief_prompt(company_name: str, domain: str, pages: list[tuple[str, str]],
                 facts: bool = False) -> str:
    text = "\n\n".join(f"PAGE {url}\n{body}" for url, body in pages)
    return f"""You are reading a company's own website to brief a salesperson.

Company as we know it: {company_name or "unknown"} ({domain})

{fence(text, "company website")}

{UNTRUSTED_RULE}

Write down only what these pages say. Never guess. Leave a field "" when the
pages don't say it. Be specific and short (a phrase or two each): name real
products, processes, places and numbers exactly as the site gives them.

Fields:
- name: the company's own name as the site writes it (not a headline or slogan)
- makes: what they manufacture or do
- industries_served: who they sell to
- processes: manufacturing processes or operations they mention (for example
  SMT assembly, CNC machining, forging, moulding, filling and packing, warehousing)
- sites: plants, factories or warehouses and where they are
- scale: employees, plant area, capacity or output, as stated
- material_flow: anything about stores, warehousing, internal logistics,
  conveyors, forklifts, automation or material handling
- growth: expansion, new plants or lines, capacity additions, new contracts
- customers: customers they name
{_facts_section() if facts else ""}
Return JSON with exactly these keys: {", ".join(FIELDS + (("facts",) if facts else ()))}"""


def _facts_section() -> str:
    from sellbird.amr import FACTS_PROMPT
    return FACTS_PROMPT + "\n"


async def build_brief(brain, company_name: str, domain: str, facts: bool = False) -> dict:
    """Read the site and summarise it. {"failed": reason} when it can't.

    With ``facts`` (an AMR code is confirmed) it also reads news pages and
    returns validated "facts", or "facts_failed" when there were none to read."""
    if not domain:
        return {"failed": "no website"}
    try:
        if facts:
            from sellbird.amr import MAX_NEWS_PAGES, NEWS_PATHS
            pages = await gather_pages(domain, NEWS_PATHS, MAX_NEWS_PAGES)
        else:
            pages = await gather_pages(domain)
    except Exception as e:  # a bad site must never stop the writer
        logger.info(f"brief: {domain} couldn't be read ({e})")
        pages = []
    if not pages:
        return {"failed": "site unreachable"}
    result = await brain.think_json(
        brief_prompt(company_name, domain, pages, facts=facts),
        session_id="sellbird-brief", agent="writer", task="company_brief",
    )
    brief = clean_brief(result)
    if not brief:
        return {"failed": "nothing usable on the site"}
    brief["sources"] = ", ".join(url for url, _ in pages)
    if facts:
        from sellbird.amr import validate_facts
        checked = validate_facts(result.get("facts") if isinstance(result, dict) else None, pages)
        if checked is None:
            brief["facts_failed"] = True
        else:
            brief["facts"] = checked
    return brief


async def ensure_brief(state, brain, company) -> dict:
    """The company's brief, built now if it's missing or stale.

    Once an AMR code is confirmed (sellbird.amr), a brief without facts is
    stale too, and a fresh brief's facts are recorded as observations."""
    if company is None:
        return {}
    from sellbird import amr
    brief, stamp = await state.get_company_brief(company.id)
    want_facts = bool(await amr.confirmed_amr_codes(state))
    if is_fresh(brief, stamp) and not (want_facts and amr.needs_facts(brief, stamp)):
        return brief
    old = brief
    brief = await build_brief(brain, company.name or "", company.domain or "", facts=want_facts)
    if getattr(brain, "paused_reason", ""):
        # The model was unavailable, not the site: don't record a failure.
        return {}
    if brief.get("failed") and old and not old.get("failed"):
        # A site that's down today doesn't undo a good brief: keep it, and
        # note the facts attempt so it's retried later, not every cycle.
        brief = {**old, "facts_failed": True} if want_facts else old
    await state.set_company_brief(company.id, brief)
    if want_facts and "facts" in brief and brief is not old:
        await amr.record_facts(state, company.id, brief["facts"])
    return brief


def clean_company_name(brief: dict, current: str) -> Optional[str]:
    """The site's own name when ours looks like a headline, else None."""
    name = (brief or {}).get("name", "").strip()
    if not name or name == current:
        return None
    looks_wrong = (len(current.split()) > 6 or re.search(r"\d{3,}", current or "")
                   or not current)
    return name if looks_wrong and len(name) <= 80 else None
