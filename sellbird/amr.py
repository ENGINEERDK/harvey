"""AMR buying signals: the facts that say whether a plant needs mobile robots.

Fork-local. Keeps the catalog, validation and collection of these signals in
one file so upstream rebases touch only one-line hooks.
"""

from __future__ import annotations

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
