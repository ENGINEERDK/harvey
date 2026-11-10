"""Company fit: one 0-100 number per company, with the reasons behind it.

Worked out on read from what Sellbird already holds (the company's industry,
its contacts and their email status, and its observations), weighed against
the ICP in the config. Nothing is stored, so the score follows the config
the moment it changes.

    sector match      20  the company's industry is one of icp.industries
    decision maker    30  a contact whose title matches icp.titles
                          (15 for another named person, 5 for a general inbox)
    reachable         25  best email: verified 25, risky 12, guess 5;
                          a contact form alone is 5
    buying signal     25  the best of: an open role matching icp.hiring_signals
                          25, an expansion dated in the last 24 months 25,
                          PLI approval 20, automation level 2+ 15, two or more
                          plants 10 (5 for a careers page with no matching role)

A company on the do-not-contact list scores 0, and so does one the AMR
brief found has no plant or warehouse of its own. Under 100 employees costs
15. AMR facts count at confidence 0.8 or more. Bands: A 70+, B 45-69, C below.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sellbird.collectors.ats import role_matches
from sellbird.collectors.people import title_matches
from sellbird.gate import do_not_contact_match

SECTOR, DECISION_MAKER, REACHABLE, BUYING = 20, 30, 25, 25
EMAIL_POINTS = {"verified": 25, "risky": 12, "guess": 5}
DEAD_STATUSES = {"lost", "opted_out"}
AMR_FLOOR = 0.8
EXPANSION_MONTHS = 24
SMALL_EMPLOYEES, SMALL_PENALTY = 100, 15


def fit_band(score: int) -> str:
    return "A" if score >= 70 else "B" if score >= 45 else "C"


def current_fact(observations: list[dict], code: str) -> list[dict]:
    """The newest reading of ``code`` (every row of it, for several values),
    when it says yes at confidence AMR_FLOOR or more; [] otherwise."""
    rows = [o for o in observations if o.get("signal_code") == code]
    if not rows:
        return []
    newest = max(o.get("observed_at") or "" for o in rows)
    rows = [o for o in rows if (o.get("observed_at") or "") == newest]
    return [o for o in rows if (o.get("value_num") is None or o.get("value_num") != 0)
            and float(o.get("confidence") if o.get("confidence") is not None else 1.0) >= AMR_FLOOR]


def recent_expansion(value_num, now: datetime | None = None) -> bool:
    """Is a YYYYMM expansion date within the last EXPANSION_MONTHS?"""
    try:
        year, month = divmod(int(value_num), 100)
    except (TypeError, ValueError):
        return False
    now = now or datetime.now(timezone.utc)
    age = (now.year - year) * 12 + (now.month - month)
    return 1 <= month <= 12 and -12 <= age <= EXPANSION_MONTHS


def _hiring_terms(icp) -> list[str]:
    return [t.strip().lower() for t in (getattr(icp, "hiring_signals", None) or []) if t.strip()]


def current_roles(observations: list[dict]) -> list[dict]:
    """HIRING_ROLE rows still current: those after the latest "board lists
    nothing relevant" row (num 0, no text), which closes earlier roles."""
    roles = sorted((o for o in observations if o.get("signal_code") == "HIRING_ROLE"),
                   key=lambda o: o.get("observed_at") or "")
    closed = [i for i, o in enumerate(roles)
              if o.get("value_num") == 0 and not (o.get("value_text") or "").strip()]
    return roles[closed[-1]:] if closed else roles


def company_fit(company: dict, prospects: list[dict], observations: list[dict],
                config, dnc: list[str] | None = None) -> tuple[int, list[str]]:
    """(score, reasons) for one company. ``prospects`` and ``observations``
    are that company's rows, as dicts. ``dnc`` is the do-not-contact list
    (default: the config's)."""
    icp = config.icp
    if dnc is None:
        dnc = getattr(getattr(config, "compliance", None), "do_not_contact", None) or []
    domain = (company.get("domain") or "").lower()
    if domain and dnc and do_not_contact_match(f"x@{domain}", dnc):
        return 0, ["on the do-not-contact list"]

    no_floor = current_fact(observations, "NO_PHYSICAL_OPERATION")
    if no_floor:
        return 0, [f"no plant or warehouse of its own ({no_floor[0].get('value_text') or 'per its site'})"]

    score, reasons = 0, []

    industries = {i.strip().lower() for i in (icp.industries or [])}
    if (company.get("industry") or "").strip().lower() in industries:
        score += SECTOR
        reasons.append(f"sector: {company['industry']}")

    # Someone who opted out or is on the list won't be written to: they
    # don't make the company reachable.
    live = [p for p in prospects if (p.get("status") or "") not in DEAD_STATUSES
            and not (dnc and p.get("email") and do_not_contact_match(p["email"], dnc))]
    named = [p for p in live if (p.get("first_name") or "").strip()]
    makers = [p for p in named if icp.titles and title_matches(p.get("title") or "", icp.titles)]
    if not makers:
        # Names the site lists that Sellbird didn't turn into contacts.
        for o in observations:
            if o.get("signal_code") == "DECISION_MAKER_TITLE":
                title = (o.get("value_text") or "").split("—", 1)[-1].strip(" ~()")
                if icp.titles and title_matches(title, icp.titles):
                    makers.append({"title": title, "first_name": ""})
                    break
    if makers:
        score += DECISION_MAKER
        reasons.append(f"decision maker: {makers[0].get('title')}")
    elif named:
        score += DECISION_MAKER // 2
        reasons.append("named contact (not a target title)")
    elif live:
        score += 5
        reasons.append("general inbox only")

    best = max((EMAIL_POINTS.get((p.get("email_status") or "").lower(), 0) for p in live), default=0)
    if best:
        score += best
        label = {25: "verified email", 12: "risky email", 5: "guessed email"}[best]
        reasons.append(label)
    elif any(o.get("signal_code") == "CONTACT_FORM_URL" for o in observations):
        score += 5
        reasons.append("contact form only")
    else:
        reasons.append("no way to reach yet")

    terms = _hiring_terms(icp)
    roles = current_roles(observations)
    hit = next((t for o in roles if (t := role_matches(o.get("value_text"), terms))), "")
    buying: list[tuple[int, str]] = []
    if hit:
        buying.append((BUYING, f"hiring: {hit}"))
    expansion = [o for o in current_fact(observations, "EXPANSION_NEWS")
                 if recent_expansion(o.get("value_num"))]
    if expansion:
        buying.append((25, f"expanding: {expansion[0].get('value_text') or 'new capacity'}"))
    if current_fact(observations, "PLI_BENEFICIARY"):
        buying.append((20, "PLI approval"))
    level = current_fact(observations, "AUTOMATION_LEVEL")
    if level and (level[0].get("value_num") or 0) >= 2:
        buying.append((15, f"automation level {int(level[0]['value_num'])}"))
    plants = current_fact(observations, "PLANT_COUNT")
    if plants and (plants[0].get("value_num") or 0) >= 2:
        buying.append((10, f"{int(plants[0]['value_num'])} plants"))
    if buying:
        points, why = max(buying, key=lambda b: b[0])
        score += points
        reasons.append(why)
    elif roles:
        score += 5
        reasons.append("careers page, no matching role")

    employees = current_fact(observations, "EMPLOYEE_BAND")
    if employees and (employees[0].get("value_num") or 0) < SMALL_EMPLOYEES:
        score -= SMALL_PENALTY
        reasons.append(f"small: {int(employees[0]['value_num'])} employees")

    return max(0, min(score, 100)), reasons

