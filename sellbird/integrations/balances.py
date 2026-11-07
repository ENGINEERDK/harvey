"""What's left on each connected API, so the owner can plan a top-up.

Every check here is a free, read-only account call: asking a service how
much is left never spends any of it. A service with no such call (Apollo)
reports only Sellbird's own monthly cap for it. Nothing is sent but the key the
service already has.

Each check returns a Balance; ``check_all`` runs the connected ones in
parallel with a short timeout, so a slow service can't hold the Settings page.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
from dataclasses import asdict, dataclass, field
from typing import Optional

import httpx

TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
LOW_SHARE = 0.10   # under 10% of the allowance left counts as low


@dataclass
class Balance:
    tool: str
    label: str                    # what the tool does for Sellbird
    connected: bool
    status: str = "unknown"       # ok | low | exhausted | unknown | error
    remaining: Optional[float] = None
    total: Optional[float] = None
    unit: str = "credits"
    resets: str = ""              # when the allowance renews, if the service says
    detail: str = ""              # one plain line for the Settings card
    harvey_cap: dict = field(default_factory=dict)   # {"used": n, "cap": n} this month
    plans_url: str = ""
    verifications_left: Optional[float] = None       # Hunter: its verifier allowance

    def to_dict(self) -> dict:
        return asdict(self)


def _status(remaining, total=None) -> str:
    if remaining is None:
        return "unknown"
    if remaining <= 0:
        return "exhausted"
    if total and remaining / total < LOW_SHARE:
        return "low"
    return "ok"


# Keys for this check: the .env file's values when the caller passes them
# (the dashboard reads .env from disk), else the process environment.
_KEYS: contextvars.ContextVar[dict] = contextvars.ContextVar("balance_keys", default={})


def _key(name: str) -> str:
    return str(_KEYS.get().get(name) or os.getenv(name, "")).strip()


# ── One check per service ──

async def reoon(client) -> Balance:
    b = Balance("reoon", "Email verification", bool(_key("REOON_API_KEY")),
                unit="checks", plans_url="https://www.reoon.com/email-verifier/")
    if not b.connected:
        return b
    resp = await client.get("https://emailverifier.reoon.com/api/v1/check-account-balance/",
                            params={"key": _key("REOON_API_KEY")})
    data = resp.json() if resp.status_code == 200 else {}
    if data.get("status") != "success":
        b.status, b.detail = "error", "Reoon didn't accept the key"
        return b
    daily = int(data.get("remaining_daily_credits") or 0)
    instant = int(data.get("remaining_instant_credits") or 0)
    b.remaining = daily + instant
    b.status = _status(b.remaining)
    b.resets = "daily credits refill every day"
    b.detail = f"{daily} daily + {instant} one-time checks left"
    return b


async def zerobounce(client) -> Balance:
    b = Balance("zerobounce", "Email verification", bool(_key("ZEROBOUNCE_API_KEY")),
                unit="checks", plans_url="https://www.zerobounce.net/email-validation-pricing")
    if not b.connected:
        return b
    resp = await client.get("https://api.zerobounce.net/v2/getcredits",
                            params={"api_key": _key("ZEROBOUNCE_API_KEY")})
    try:
        credits = int(float((resp.json() or {}).get("Credits")))
    except (TypeError, ValueError, AttributeError):
        credits = None
    if resp.status_code != 200 or credits is None or credits < 0:
        b.status, b.detail = "error", "ZeroBounce didn't accept the key"
        return b
    b.remaining = credits
    b.status = _status(credits)
    b.detail = f"{credits} checks left"
    return b


async def hunter(client, state=None) -> Balance:
    b = Balance("hunter", "Email patterns, verification, finder", bool(_key("HUNTER_API_KEY")),
                unit="searches", plans_url="https://hunter.io/pricing")
    if not b.connected:
        return b
    resp = await client.get("https://api.hunter.io/v2/account",
                            params={"api_key": _key("HUNTER_API_KEY")})
    data = (resp.json() or {}).get("data") if resp.status_code == 200 else None
    if not isinstance(data, dict):
        b.status, b.detail = "error", "Hunter didn't accept the key"
        return b
    req = data.get("requests") or {}
    searches = req.get("searches") or {}
    verifs = req.get("verifications") or {}
    s_left = (searches.get("available") or 0) - (searches.get("used") or 0)
    v_left = (verifs.get("available") or 0) - (verifs.get("used") or 0)
    b.remaining, b.total = s_left, searches.get("available") or None
    b.verifications_left = v_left
    b.status = _status(s_left, b.total)
    b.resets = data.get("reset_date") or ""
    b.detail = (f"{s_left} of {searches.get('available', 0)} searches and "
                f"{v_left} of {verifs.get('available', 0)} verifications left "
                f"({data.get('plan_name') or 'plan'})")
    if state is not None:
        from sellbird.integrations.email_finder import _hunter_monthly_cap, _hunter_usage_key
        b.harvey_cap = {"used": int(await state.get_setting(_hunter_usage_key()) or 0),
                        "cap": _hunter_monthly_cap()}
    return b


async def prospeo(client, state=None) -> Balance:
    b = Balance("prospeo", "Email finder", bool(_key("PROSPEO_API_KEY")),
                plans_url="https://prospeo.io/pricing")
    if not b.connected:
        return b
    resp = await client.get("https://api.prospeo.io/account-information",
                            headers={"X-KEY": _key("PROSPEO_API_KEY")})
    body = resp.json() if resp.status_code == 200 else {}
    data = body.get("response") if isinstance(body, dict) and not body.get("error") else None
    if not isinstance(data, dict):
        b.status, b.detail = "error", "Prospeo didn't accept the key"
        return b
    left, used = data.get("remaining_credits"), data.get("used_credits") or 0
    b.remaining = left
    b.total = (left or 0) + used or None
    b.status = _status(left, b.total)
    b.resets = (data.get("next_quota_renewal_date") or "")[:10]
    b.detail = f"{left} credits left ({str(data.get('current_plan') or 'plan').title()})"
    await _finder_cap(b, state)
    return b


async def apollo(client, state=None) -> Balance:
    """Apollo has no API for credits left: show Sellbird's own cap, and point
    to where the balance is."""
    b = Balance("apollo", "Email finder", bool(_key("APOLLO_API_KEY")),
                plans_url="https://www.apollo.io/pricing")
    if not b.connected:
        return b
    b.detail = "Apollo doesn't report credits by API: see Settings → Plans & Billing in Apollo"
    await _finder_cap(b, state)
    cap = b.harvey_cap
    if cap and cap.get("cap"):
        b.status = "exhausted" if cap["used"] >= cap["cap"] else _status(cap["cap"] - cap["used"], cap["cap"])
    return b


async def serper(client) -> Balance:
    b = Balance("serper", "Web search for discovery", bool(_key("SERPER_API_KEY")),
                unit="searches", plans_url="https://serper.dev")
    if not b.connected:
        return b
    resp = await client.get("https://google.serper.dev/account",
                            headers={"X-API-KEY": _key("SERPER_API_KEY")})
    data = resp.json() if resp.status_code == 200 else {}
    if not isinstance(data, dict) or "balance" not in data:
        b.status, b.detail = "error", "Serper didn't accept the key"
        return b
    b.remaining = data.get("balance")
    b.status = _status(b.remaining)
    b.detail = f"{b.remaining} searches left"
    return b


async def _finder_cap(b: Balance, state) -> None:
    """Sellbird's own monthly cap for an email finder, when that feature exists."""
    if state is None:
        return
    try:
        from sellbird.integrations import email_finders as finders
    except ImportError:
        return
    if b.tool in getattr(finders, "CAP_ENV", {}):
        b.harvey_cap = {"used": int(await state.get_setting(finders.usage_key(b.tool)) or 0),
                        "cap": finders.monthly_cap(b.tool)}


CHECKS = {
    "reoon": lambda c, s: reoon(c),
    "zerobounce": lambda c, s: zerobounce(c),
    "hunter": hunter,
    "prospeo": prospeo,
    "apollo": apollo,
    "serper": lambda c, s: serper(c),
}


NAMES = {"reoon": "Reoon", "zerobounce": "ZeroBounce", "hunter": "Hunter",
         "prospeo": "Prospeo", "apollo": "Apollo", "serper": "Serper"}


def needs_action(rows: list[dict]) -> list[str]:
    """Short notes on what's run out or is running low, and what that means
    for Sellbird, for the top of the Settings card. Empty when all is well."""
    on = {r["tool"]: r for r in rows if r.get("connected")}
    notes = []

    def gone(r):
        return r.get("status") == "exhausted"

    # Verification: Reoon, ZeroBounce, and Hunter's verifications.
    verifiers = [t for t in ("reoon", "zerobounce", "hunter") if t in on]
    if verifiers:
        empty = [t for t in verifiers
                 if (t == "hunter" and (on[t].get("verifications_left") or 0) <= 0)
                 or (t != "hunter" and gone(on[t]))]
        if len(empty) == len(verifiers):
            notes.append(f"No email verifier has checks left ({', '.join(NAMES[t] for t in empty)}): "
                         "new addresses stay 'guess' and aren't sent. Top up Reoon or add a "
                         "ZeroBounce key.")
    for tool, what in (("serper", "discovery searches can't run"),
                       ("prospeo", "Sellbird skips it as an email finder"),
                       ("apollo", "Sellbird skips it as an email finder"),
                       ("hunter", "no pattern lookups or finder searches")):
        r = on.get(tool)
        if not r:
            continue
        renews = f" (renews {r['resets']})" if r.get("resets") and r["resets"][:1].isdigit() else ""
        if gone(r):
            notes.append(f"{NAMES[tool]} is used up: {what} until it renews or you top up{renews}.")
        elif r.get("status") == "low":
            left = r.get("remaining")
            notes.append(f"{NAMES[tool]} is running low: {int(left) if left is not None else 'few'} "
                         f"{r.get('unit') or 'credits'} left{renews}.")
    for r in on.values():
        if r.get("status") == "error":
            notes.append(f"{NAMES.get(r['tool'], r['tool'])}: couldn't check the balance. "
                         "Is the key still valid?")
    return notes


async def check_all(state=None, keys: dict | None = None) -> list[dict]:
    """Every service Sellbird can use, connected or not, with what's left.
    ``keys`` (e.g. the .env file's values) win over the process environment."""
    _KEYS.set(dict(keys or {}))
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        async def one(name):
            try:
                return await asyncio.wait_for(CHECKS[name](client, state), 15)
            except Exception:
                return Balance(name, "", True, status="error",
                               detail="Couldn't reach the service just now")
        results = await asyncio.gather(*(one(n) for n in CHECKS))
    return [r.to_dict() for r in results]
