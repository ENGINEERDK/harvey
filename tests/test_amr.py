"""AMR buying signals (sellbird/amr.py)."""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from sellbird import amr
from sellbird.fit import current_roles
from sellbird.signals import seed_signal_catalog
from sellbird.state import StateManager

CONFIG = SimpleNamespace(icp=SimpleNamespace(hiring_signals=["forklift operator"], titles=[]))


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "t.db"))
    await s.init_db()
    await seed_signal_catalog(s)
    return s


def _row(company, text, url="https://acme.example.com/careers", at="2026-11-01 10:00:00"):
    return {"company_id": company, "signal_code": "HIRING_ROLE", "collector": "profile",
            "value_text": text, "evidence_url": url, "observed_at": at}


async def _current(state, company):
    rows = await state.get_observations(company_id=company, signal_code="HIRING_ROLE")
    return sorted(o["value_text"] for o in current_roles(rows) if o["value_text"])


@pytest.mark.asyncio
async def test_junk_is_closed_and_a_real_role_beside_it_survives(state):
    await state.add_observations([_row("c1", "Apply Now"), _row("c1", "Forklift Operator")])
    assert await amr.close_junk_hiring_rows(state, CONFIG) == (1, 1)
    assert await _current(state, "c1") == ["Forklift Operator"]


@pytest.mark.asyncio
async def test_a_company_with_only_junk_ends_with_none(state):
    await state.add_observations([_row("c1", "Sales Terms & Conditions")])
    await amr.close_junk_hiring_rows(state, CONFIG)
    assert await _current(state, "c1") == []


@pytest.mark.asyncio
async def test_ats_board_rows_are_left_alone(state):
    await state.add_observations([_row("c1", "Operator, Forklift", url="https://jobs.lever.co/acme/1")])
    assert await amr.close_junk_hiring_rows(state, CONFIG) == (0, 0)
    assert await _current(state, "c1") == ["Operator, Forklift"]


@pytest.mark.asyncio
async def test_running_twice_changes_nothing(state):
    await state.add_observations([_row("c1", "Apply Now")])
    await amr.close_junk_hiring_rows(state, CONFIG)
    assert await amr.close_junk_hiring_rows(state, CONFIG) == (0, 0)
