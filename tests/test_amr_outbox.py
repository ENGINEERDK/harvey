"""Cold email to a contact gated after it was drafted (sellbird.amr, D19)."""

import pytest
import pytest_asyncio

from sellbird import amr
from sellbird.state import StateManager
from tests.test_outbox_native import Env, make_sender, seed_prospect


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "t.db"))
    await s.init_db()
    return s


class GatedCfg:
    class icp:
        exclude_titles = ["CFO"]


async def _queued(state, title):
    pid = await seed_prospect(state)
    async with state._connect() as db:
        await db.execute("UPDATE prospects SET title = ? WHERE id = ?", (title, pid))
        await db.commit()
    item_id = await state.add_outbox_item(prospect_id=pid, to_email="jane@acme.com",
                                          subject="Hello", body="Hi Jane", send_at="2026-01-01",
                                          status="approved", approved_by="you")
    return pid, item_id


def _sender(state):
    sender = make_sender(state, provider=None)
    sender.config.icp = GatedCfg.icp
    return sender


@pytest.mark.asyncio
async def test_a_gated_contacts_queued_email_goes_back_for_review(state):
    pid, item_id = await _queued(state, "CFO")
    sender = _sender(state)
    item = await state.get_outbox_item(item_id)
    verdict = await sender._blocked(item, await state.get_prospect(pid), allow_risky=False)
    assert verdict[0] == "pending_review" and verdict[1].startswith(amr.GATE_HELD)


@pytest.mark.asyncio
async def test_approve_all_skips_it_and_a_hand_approval_sends_it(state):
    pid, item_id = await _queued(state, "CFO")
    await state.update_outbox_item(item_id, status="pending_review", error=amr.GATE_HELD + "title")
    assert await state.approve_outbox() == 0                 # approve-all leaves it
    assert await state.approve_outbox(item_id) == 1          # a person approves it
    item = await state.get_outbox_item(item_id)
    assert item["gate_override_at"]
    verdict = await _sender(state)._blocked(item, await state.get_prospect(pid), allow_risky=False)
    assert not (verdict and verdict[1].startswith(amr.GATE_HELD))


@pytest.mark.asyncio
async def test_an_ungated_contact_is_not_held(state):
    pid, item_id = await _queued(state, "Plant Head")
    item = await state.get_outbox_item(item_id)
    verdict = await _sender(state)._blocked(item, await state.get_prospect(pid), allow_risky=False)
    assert not (verdict and verdict[1].startswith(amr.GATE_HELD))
