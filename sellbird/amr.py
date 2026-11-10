"""AMR buying signals: the facts that say whether a plant needs mobile robots.

Fork-local. Keeps the catalog, validation and collection of these signals in
one file so upstream rebases touch only one-line hooks.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sellbird.collectors.ats import detect_board, role_keywords, role_matches
from sellbird.fit import current_roles


def _stamp(moment: datetime) -> str:
    """The format SQLite's CURRENT_TIMESTAMP writes, so rows sort together."""
    return moment.strftime("%Y-%m-%d %H:%M:%S")


async def close_junk_hiring_rows(state, config) -> tuple[int, int]:
    """Close current HIRING_ROLE rows that aren't a role we look for.

    Writes a closing row (num 0, no text) and then re-adds the rows that do
    match, a second later, so a real role isn't closed with the junk beside
    it. Rows from an ATS board are left as they are: those were matched with
    any word order. Returns (companies changed, rows closed).
    """
    terms = role_keywords(getattr(config, "icp", None))
    rows = await state.get_observations(signal_code="HIRING_ROLE", limit=1_000_000)
    by_company: dict[str, list[dict]] = {}
    for row in rows:
        by_company.setdefault(row["company_id"], []).append(row)

    now = datetime.now(timezone.utc)
    changed = closed = 0
    writes: list[dict] = []
    for company_id, observations in by_company.items():
        listed = [o for o in current_roles(observations) if (o.get("value_text") or "").strip()]
        keep = [o for o in listed
                if role_matches(o["value_text"], terms)
                or detect_board(o.get("evidence_url") or "", "")]
        if len(keep) == len(listed):
            continue
        changed += 1
        closed += len(listed) - len(keep)
        writes.append({"company_id": company_id, "signal_code": "HIRING_ROLE",
                       "collector": "backfill", "value_num": 0,
                       "observed_at": _stamp(now)})
        writes.extend({"company_id": company_id, "signal_code": "HIRING_ROLE",
                       "collector": o.get("collector", ""), "value_text": o["value_text"],
                       "value_num": o.get("value_num"),
                       "confidence": o.get("confidence", 1.0),
                       "evidence_url": o.get("evidence_url", ""),
                       "observed_at": _stamp(now + timedelta(seconds=1))} for o in keep)
    await state.add_observations(writes)
    return changed, closed


# ── The catalog: proposed like every signal, confirmed as a set ──

_COST = "one Claude call per company (its company brief)"
AMR_SIGNALS: list[dict] = [
    {"code": "EXPANSION_NEWS", "label": "Expanding (new plant, line or capacity)",
     "description": ("The company's own pages announce a new plant, line, warehouse or capacity "
                     "addition, with a date. Counts for 24 months from that date."),
     "value_type": "num"},
    {"code": "PLI_BENEFICIARY", "label": "Approved under a PLI scheme",
     "description": "The site says the company is approved under a Production Linked Incentive scheme.",
     "value_type": "text"},
    {"code": "PLANT_COUNT", "label": "Plants and warehouses",
     "description": "How many plants, factories or warehouses the site says the company runs.",
     "value_type": "num"},
    {"code": "INTRALOGISTICS", "label": "Internal material movement",
     "description": ("The site describes stores, line feeding, kitting, forklifts, conveyors or its "
                     "own distribution centre: material that has to move inside the plant."),
     "value_type": "text"},
    {"code": "AUTOMATION_LEVEL", "label": "Automation level (0-3)",
     "description": ("How automated the site says the operation is: ERP or SAP, MES, Industry 4.0, "
                     "robots, automated lines. 0 none mentioned, 3 heavily automated."),
     "value_type": "num"},
    {"code": "EXISTING_AGV", "label": "Already runs AGVs or AMRs",
     "description": "The site says AGVs or mobile robots already run in its plant or warehouse.",
     "value_type": "text"},
    {"code": "CERTIFICATION", "label": "Quality certifications",
     "description": "Certifications the site names: IATF 16949, ISO 9001/14001/45001, USFDA, WHO-GMP, AS9100.",
     "value_type": "text"},
    {"code": "MNC_SUBSIDIARY", "label": "Part of a multinational",
     "description": "The company says it is the subsidiary or group company of a multinational.",
     "value_type": "text"},
    {"code": "EMPLOYEE_BAND", "label": "Employees",
     "description": "How many people the site says the company employs.",
     "value_type": "num"},
    {"code": "NO_PHYSICAL_OPERATION", "label": "No plant or warehouse of its own",
     "description": ("A pure trader, agent, consultant or software firm, with no floor of its own a "
                     "robot could work on. Companies with it are not emailed."),
     "value_type": "text"},
]
for _sig in AMR_SIGNALS:
    _sig.update(category="profile", collector="amr_brief", cost_note=_COST, confidence_floor=0.0)
AMR_CODES = tuple(s["code"] for s in AMR_SIGNALS)

CONFIDENCE = 0.9
MANUAL = "manual"          # collector of a fact a person cleared (sellbird amr clear)
NEWS_PATHS = ("/news", "/press", "/newsroom", "/media", "/investors", "/press-releases")
MAX_NEWS_PAGES = 2
CERTIFICATIONS = ("IATF 16949", "ISO 9001", "ISO 14001", "ISO 45001", "USFDA", "WHO-GMP", "AS9100")
OPERATION_KINDS = ("trader", "agent", "consultant", "software", "other")


async def confirmed_amr_codes(state) -> set[str]:
    """The AMR codes the user has confirmed; empty on an install that hasn't."""
    if state is None:
        return set()
    rows = await state.get_signal_codes(status="confirmed")
    return {r["code"] for r in rows} & set(AMR_CODES)


FACTS_PROMPT = f"""
Also return "facts": an object with these keys, each null unless the pages
say it. Every non-null value carries "quote": the exact words from one page
that say it, copied character for character (at most 300 characters).
- expansion: {{"what": ..., "date": "YYYY-MM" or "YYYY", "quote": ...}} a new plant,
  line, warehouse or capacity addition, only when the pages give its date
- pli: {{"scheme": ..., "quote": ...}} approved under a Production Linked Incentive scheme
- plant_count: {{"value": integer, "quote": ...}} plants, factories or warehouses they run
- intralogistics: {{"what": ..., "quote": ...}} stores, line feeding, kitting, forklifts,
  conveyors, their own distribution centre
- automation_level: {{"value": 0 to 3, "quote": ...}} 0 none, 1 ERP/SAP, 2 MES or
  automated lines, 3 robots or Industry 4.0 across the plant
- existing_agv: {{"what": ..., "quote": ...}} AGVs or mobile robots already running there
- certifications: {{"names": [any of {", ".join(CERTIFICATIONS)}], "quote": ...}}
- mnc_parent: {{"name": ..., "quote": ...}} the multinational it belongs to
- employees: {{"value": integer, "quote": ...}}
- no_physical_operation: {{"kind": one of {", ".join(OPERATION_KINDS)}, "quote": ...}}
  only when the company has no plant or warehouse of its own: a pure trader,
  agent, consultant or software firm. A distributor or logistics company with
  its own warehouse HAS one."""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _source(quote, pages) -> str:
    """The page whose text holds ``quote``, or "" when none does."""
    q = _norm(str(quote or ""))[:300]
    if len(q) < 8:
        return ""
    return next((url for url, body in pages if q in _norm(body)), "")


def _text(value, cap=200) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:cap]


def _int(value, low, high):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high and float(value) == number else None


def _yyyymm(value) -> int | None:
    m = re.fullmatch(r"(20\d\d)(?:-(0[1-9]|1[0-2]))?", str(value or "").strip())
    if not m:
        return None
    # A year alone counts from January, so the 24-month window never runs long.
    return int(m.group(1)) * 100 + int(m.group(2) or 1)


def validate_facts(raw, pages) -> dict | None:
    """Facts that pass the schema AND are quoted from a page Sellbird read.

    None when ``raw`` isn't a facts object at all (the brief's facts failed);
    {} when it is but nothing survived. Each kept fact carries "evidence_url":
    the page its quote was found on, never a URL the model gave."""
    if not isinstance(raw, dict):
        return None
    out: dict = {}

    def keep(key, item, **values):
        url = _source(item.get("quote"), pages)
        if url and all(v not in (None, "", []) for v in values.values()):
            out[key] = {**values, "quote": _text(item.get("quote"), 300), "evidence_url": url}

    for key, item in raw.items():
        if not isinstance(item, dict):
            continue
        if key == "expansion":
            keep(key, item, what=_text(item.get("what")), date=_yyyymm(item.get("date")))
        elif key == "pli":
            keep(key, item, scheme=_text(item.get("scheme")) or "PLI")
        elif key == "plant_count":
            keep(key, item, value=_int(item.get("value"), 1, 200))
        elif key == "intralogistics":
            keep(key, item, what=_text(item.get("what")))
        elif key == "automation_level":
            keep(key, item, value=_int(item.get("value"), 0, 3))
        elif key == "existing_agv":
            keep(key, item, what=_text(item.get("what")) or "AGV/AMR")
        elif key == "certifications":
            names = [n for n in CERTIFICATIONS
                     if any(_norm(n) == _norm(str(x)) for x in (item.get("names") or []))]
            keep(key, item, names=names)
        elif key == "mnc_parent":
            keep(key, item, name=_text(item.get("name")))
        elif key == "employees":
            keep(key, item, value=_int(item.get("value"), 1, 500_000))
        elif key == "no_physical_operation":
            kind = str(item.get("kind") or "").strip().lower()
            keep(key, item, kind=kind if kind in OPERATION_KINDS else None)
    return out


def facts_to_rows(company_id: str, facts: dict) -> list[dict]:
    """Observation rows for validated facts, one code each (certifications: one per name)."""
    rows = []

    def row(code, num=None, text=""):
        rows.append({"company_id": company_id, "signal_code": code, "collector": "amr_brief",
                     "value_num": num, "value_text": text, "confidence": CONFIDENCE,
                     "evidence_url": fact["evidence_url"]})

    for key, fact in facts.items():
        if key == "expansion":
            row("EXPANSION_NEWS", fact["date"], fact["what"])
        elif key == "pli":
            row("PLI_BENEFICIARY", None, fact["scheme"])
        elif key == "plant_count":
            row("PLANT_COUNT", fact["value"])
        elif key == "intralogistics":
            row("INTRALOGISTICS", None, fact["what"])
        elif key == "automation_level":
            row("AUTOMATION_LEVEL", fact["value"])
        elif key == "existing_agv":
            row("EXISTING_AGV", None, fact["what"])
        elif key == "certifications":
            for name in fact["names"]:
                row("CERTIFICATION", None, name)
        elif key == "mnc_parent":
            row("MNC_SUBSIDIARY", None, fact["name"])
        elif key == "employees":
            row("EMPLOYEE_BAND", fact["value"])
        elif key == "no_physical_operation":
            row("NO_PHYSICAL_OPERATION", None, fact["kind"])
    return rows


async def record_facts(state, company_id: str, facts: dict) -> int:
    """Write a fresh brief's facts as observations, for confirmed codes only.

    A code the last brief had and this one doesn't gets a closing row (it
    stops being current). A code a person cleared is never written again."""
    confirmed = await confirmed_amr_codes(state)
    if not confirmed:
        return 0
    newest: dict[str, dict] = {}
    for o in await state.get_observations(company_id=company_id, limit=5000):
        if o["signal_code"] in confirmed and o["signal_code"] not in newest:
            newest[o["signal_code"]] = o       # get_observations is newest first
    stamp = _stamp(datetime.now(timezone.utc))
    rows = [r for r in facts_to_rows(company_id, facts)
            if r["signal_code"] in confirmed
            and (newest.get(r["signal_code"]) or {}).get("collector") != MANUAL]
    found = {r["signal_code"] for r in rows}
    for code, last in newest.items():
        if (code not in found and last.get("collector") == "amr_brief"
                and (last.get("value_num") is None or last.get("value_num") != 0)):
            rows.append({"company_id": company_id, "signal_code": code,
                         "collector": "amr_brief", "value_num": 0, "confidence": CONFIDENCE})
    for r in rows:
        r["observed_at"] = stamp
    return await state.add_observations(rows)


def needs_facts(brief: dict, stamp: str | None) -> bool:
    """Should this brief be rebuilt to get facts? Yes when it has none, unless
    a facts attempt failed less than BRIEF_RETRY_DAYS ago."""
    from sellbird.company_brief import BRIEF_RETRY_DAYS, _age_days
    if "facts" in brief:
        return False
    if brief.get("facts_failed") or brief.get("failed"):
        age = _age_days(stamp)
        return age is None or age >= BRIEF_RETRY_DAYS
    return True


BRIEFS_PER_CYCLE = 10


async def fit_scores(state, config) -> dict[str, int]:
    """{company_id: fit} for every company, read in one pass (sellbird.register)."""
    from sellbird.register import build_register, sqlite_query
    rows = await build_register(sqlite_query(state.db_path), config)
    return {r["id"]: r["fit"] for r in rows}


async def brief_queue(state, config, limit: int = BRIEFS_PER_CYCLE) -> list:
    """Companies whose brief needs facts: those briefed before facts existed
    first (they're the ones being emailed), then the rest, best fit first."""
    from sellbird.company_brief import parse_brief
    async with state._connect() as db:
        async with db.execute(
            "SELECT id, brief_json, brief_at FROM companies WHERE COALESCE(domain, '') != ''"
        ) as cursor:
            rows = await cursor.fetchall()
    fits = await fit_scores(state, config)
    pre, rest = [], []
    for cid, raw, stamp in rows:
        brief = parse_brief(raw)
        if not needs_facts(brief, stamp):
            continue
        (pre if brief and not brief.get("failed") and not brief.get("facts_failed")
         else rest).append(cid)
    rest.sort(key=lambda c: fits.get(c, 0), reverse=True)
    pre.sort(key=lambda c: fits.get(c, 0), reverse=True)
    return (pre + rest)[:limit]


async def brief_step(state, brain, config, limit: int = BRIEFS_PER_CYCLE) -> int:
    """Brief up to ``limit`` companies for their AMR facts. No Claude call
    unless an AMR code is confirmed; stops when the model becomes unusable."""
    from sellbird.company_brief import ensure_brief
    if not await confirmed_amr_codes(state):
        return 0
    done = 0
    for company_id in await brief_queue(state, config, limit):
        company = await state.get_company(company_id)
        if company is None:
            continue
        await ensure_brief(state, brain, company)
        if getattr(brain, "paused_reason", ""):
            break
        done += 1
    return done


# ── Who gets written to (shared by the writer and the heartbeat's count) ──

DRAFT_DAYS = 3


async def no_floor_companies(state) -> set[str]:
    """Companies whose current NO_PHYSICAL_OPERATION fact holds (confidence 0.8+)."""
    from sellbird.fit import current_fact
    rows = await state.get_observations(signal_code="NO_PHYSICAL_OPERATION", limit=1_000_000)
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["company_id"], []).append(r)
    return {cid for cid, obs in by.items() if current_fact(obs, "NO_PHYSICAL_OPERATION")}


def gate_reason(title: str, company_id: str, config, no_floor: set[str]) -> str:
    """Why this contact isn't written to, or "" when they can be."""
    from sellbird.collectors.people import title_matches
    excluded = getattr(getattr(config, "icp", None), "exclude_titles", None) or []
    if excluded and title and title_matches(title, excluded):
        return f"title not targeted: {title}"
    if company_id and company_id in no_floor:
        return "no plant or warehouse of its own"
    return ""


def min_write_score(config) -> int:
    email = getattr(getattr(config, "channels", None), "email", None)
    try:
        return max(0, int(getattr(email, "min_write_score", 0) or 0))
    except (TypeError, ValueError):
        return 0


async def unsent_cold(state) -> int:
    async with state._connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM outbox WHERE kind = 'sequence' "
            "AND status IN ('pending_review', 'approved')"
        ) as cursor:
            return (await cursor.fetchone())[0]


def draft_room(config, unsent: int) -> int | None:
    """How many more people can be drafted for: DRAFT_DAYS of today's cold
    cap, less what's already waiting. None when there is no cap to go by."""
    from sellbird.config import cold_daily_cap
    email = getattr(getattr(config, "channels", None), "email", None)
    if email is None or not hasattr(email, "max_daily_sends"):
        return None
    return max(0, DRAFT_DAYS * cold_daily_cap(email) - unsent)


async def select_for_writing(state, brain, config, prospects: list) -> list:
    """The prospects to draft for now: best company fit first, none whose title
    or company is gated, and no more than draft_room allows. With the AMR
    codes confirmed, a candidate's company is briefed first, so a company
    found to have no floor is never drafted for."""
    from sellbird.company_brief import ensure_brief
    if not prospects:
        return []
    room = draft_room(config, await unsent_cold(state))
    if room == 0:
        return []
    fits = await fit_scores(state, config)
    ordered = sorted(prospects, key=lambda p: fits.get(p.company_id or "", 0), reverse=True)
    briefing = bool(await confirmed_amr_codes(state)) and getattr(
        getattr(config, "writer", None), "company_brief", False)
    no_floor = await no_floor_companies(state)
    chosen = []
    for p in ordered:
        if room is not None and len(chosen) >= room:
            break
        if gate_reason(p.title or "", "", config, set()):
            continue
        if briefing and p.company_id and p.company_id not in no_floor:
            company = await state.get_company(p.company_id)
            await ensure_brief(state, brain, company)
            if getattr(brain, "paused_reason", ""):
                break
            no_floor = await no_floor_companies(state)
        if gate_reason("", p.company_id or "", config, no_floor):
            continue
        chosen.append(p)
    return chosen
