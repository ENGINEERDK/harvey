"""Settings → API usage & limits: what's left on each connected service,
read with free account calls, cached, and never showing a key."""

import httpx
import pytest
from fastapi.testclient import TestClient

from sellbird.integrations import balances

REAL_CLIENT = httpx.AsyncClient
KEYS = {"REOON_API_KEY": "reoon-secret-key", "HUNTER_API_KEY": "hunter-secret-key",
        "PROSPEO_API_KEY": "prospeo-secret-key", "APOLLO_API_KEY": "apollo-secret-key",
        "SERPER_API_KEY": "serper-secret-key", "ZEROBOUNCE_API_KEY": "zb-secret-key"}


def reply(request):
    host, path = request.url.host, request.url.path
    if host == "emailverifier.reoon.com":
        return httpx.Response(200, json={"status": "success", "remaining_daily_credits": 0,
                                         "remaining_instant_credits": 0})
    if host == "api.zerobounce.net":
        return httpx.Response(200, json={"Credits": "4"})
    if host == "api.hunter.io":
        return httpx.Response(200, json={"data": {"plan_name": "Free", "reset_date": "2026-12-01",
            "requests": {"searches": {"used": 46, "available": 50},
                         "verifications": {"used": 10, "available": 100}}}})
    if host == "api.prospeo.io":
        return httpx.Response(200, json={"error": False, "response": {
            "current_plan": "FREE", "remaining_credits": 91, "used_credits": 9,
            "next_quota_renewal_date": "2026-12-03 11:52:08+00:00"}})
    if host == "google.serper.dev":
        return httpx.Response(200, json={"balance": 1948, "rateLimit": 5})
    return httpx.Response(404)


@pytest.fixture
def serve(monkeypatch):
    seen = []

    def wrapped(request):
        seen.append(request)
        return reply(request)
    monkeypatch.setattr(balances.httpx, "AsyncClient",
                        lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(wrapped), **kw))
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    return seen


@pytest.mark.asyncio
async def test_each_service_and_its_status(serve):
    rows = {r["tool"]: r for r in await balances.check_all(keys=KEYS)}
    assert rows["reoon"]["status"] == "exhausted" and "0 daily + 0 one-time" in rows["reoon"]["detail"]
    assert rows["zerobounce"]["status"] == "ok" and rows["zerobounce"]["remaining"] == 4
    assert rows["hunter"]["status"] == "low"            # 4 of 50 searches left
    assert rows["hunter"]["resets"] == "2026-12-01"
    assert rows["prospeo"]["status"] == "ok" and rows["prospeo"]["resets"] == "2026-12-03"
    assert rows["serper"]["remaining"] == 1948
    assert rows["apollo"]["status"] == "unknown" and "Plans & Billing" in rows["apollo"]["detail"]
    assert not any(r.url.host.endswith("apollo.io") for r in serve)   # no Apollo call at all


@pytest.mark.asyncio
async def test_unconnected_services_make_no_calls(serve):
    rows = {r["tool"]: r for r in await balances.check_all(keys={"SERPER_API_KEY": "s"})}
    assert rows["serper"]["connected"] and not rows["reoon"]["connected"]
    assert [r.url.host for r in serve] == ["google.serper.dev"]


@pytest.mark.asyncio
async def test_a_failing_service_is_reported_not_raised(serve, monkeypatch):
    def broken(request):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(balances.httpx, "AsyncClient",
                        lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(broken), **kw))
    rows = {r["tool"]: r for r in await balances.check_all(keys={"SERPER_API_KEY": "s"})}
    assert rows["serper"]["status"] == "error"


def test_status_thresholds():
    assert balances._status(None) == "unknown"
    assert balances._status(0, 50) == "exhausted"
    assert balances._status(4, 50) == "low"
    assert balances._status(30, 50) == "ok"


@pytest.fixture
def client(tmp_path, monkeypatch, serve):
    import sellbird.dashboard as dash
    (tmp_path / "data").mkdir()
    env = tmp_path / ".env"
    env.write_text("".join(f"{k}={v}\n" for k, v in KEYS.items()))
    monkeypatch.setattr(dash, "ENV_FILE", env)
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "data" / "harvey.db")
    monkeypatch.setattr(dash, "PID_FILE", tmp_path / "data" / "harvey.pid")
    monkeypatch.setattr(dash, "LOG_FILE", tmp_path / "data" / "harvey.log")
    monkeypatch.setattr(dash, "_api_usage_cache", {"at": None, "rows": None})
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {dash.token_store.get()}"}) as c:
        c.seen = serve
        yield c


def test_settings_usage_is_cached_and_keyless(client):
    first = client.get("/api/settings/usage").json()
    calls = len(client.seen)
    assert {s["tool"] for s in first["services"] if s["connected"]} == {
        "reoon", "zerobounce", "hunter", "prospeo", "apollo", "serper"}
    client.get("/api/settings/usage")
    assert len(client.seen) == calls                      # served from the cache
    client.get("/api/settings/usage?refresh=true")
    assert len(client.seen) == calls * 2                  # Refresh asks again
    assert not any(v in str(first) for v in KEYS.values())


# ── Needs action: what's run out, and what it means for Sellbird ──

def row(tool, status="ok", **kw):
    return {"tool": tool, "connected": True, "status": status, "unit": kw.pop("unit", "credits"),
            "resets": kw.pop("resets", ""), **kw}


def test_no_verifier_left_is_one_note():
    notes = balances.needs_action([
        row("reoon", "exhausted"), row("hunter", "exhausted", verifications_left=0, remaining=0,
                                        unit="searches", resets="2026-12-01")])
    assert notes[0].startswith("No email verifier has checks left (Reoon, Hunter)")
    assert "aren't sent" in notes[0]
    assert any(n.startswith("Hunter is used up") and "renews 2026-12-01" in n for n in notes)


def test_one_verifier_left_is_enough():
    notes = balances.needs_action([row("reoon", "exhausted"), row("zerobounce", "ok", remaining=90)])
    assert not any("No email verifier" in n for n in notes)


def test_low_and_error_notes():
    notes = balances.needs_action([row("prospeo", "low", remaining=8, resets="2026-12-03"),
                                   row("serper", "error")])
    assert "Prospeo is running low: 8 credits left (renews 2026-12-03)." in notes
    assert any(n.startswith("Serper: couldn't check") for n in notes)


def test_all_well_means_no_notes():
    assert balances.needs_action([row("reoon", "ok", remaining=500), row("serper", "ok")]) == []
    assert balances.needs_action([{"tool": "reoon", "connected": False, "status": "unknown"}]) == []


def test_settings_usage_carries_the_notes(client):
    notes = client.get("/api/settings/usage").json()["needs_action"]
    # The fake has Reoon empty but ZeroBounce with 4 checks: still a verifier.
    assert not any("No email verifier" in n for n in notes)
    assert "Hunter is running low: 4 searches left (renews 2026-12-01)." in notes
