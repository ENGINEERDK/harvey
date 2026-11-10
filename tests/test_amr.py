"""AMR buying signals (sellbird/amr.py)."""

import asyncio

import pytest
import pytest_asyncio

from sellbird import amr
from sellbird.fit import current_roles
from sellbird.signals import seed_signal_catalog
from sellbird.state import StateManager

from types import SimpleNamespace  # noqa: E402

CONFIG = SimpleNamespace(
    icp=SimpleNamespace(industries=[], titles=[], hiring_signals=["forklift operator"]),
    compliance=SimpleNamespace(do_not_contact=[]))


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


# ── Facts: schema, quote check, recording ──

from sellbird import company_brief as cb  # noqa: E402

PAGES = [("https://acme.example.com", "Acme makes gear boxes. Our new plant at Ranjangaon "
          "starts production in 2025. We run 3 manufacturing units with 900 employees."),
         ("https://acme.example.com/news", "Acme is approved under the PLI scheme for auto components.")]


def test_a_fact_is_kept_only_when_its_quote_is_on_a_page():
    facts = amr.validate_facts({
        "expansion": {"what": "new plant", "date": "2025-03", "quote": "Our new plant at Ranjangaon"},
        "pli": {"scheme": "auto components", "quote": "approved under the PLI scheme"},
        "employees": {"value": 900, "quote": "We run 3 plants and 5000 people"},   # not on a page
    }, PAGES)
    assert set(facts) == {"expansion", "pli"}
    assert facts["expansion"]["date"] == 202503
    assert facts["pli"]["evidence_url"] == "https://acme.example.com/news"


@pytest.mark.parametrize("raw,key", [
    ({"plant_count": {"value": 0, "quote": "3 manufacturing units"}}, "plant_count"),
    ({"plant_count": {"value": "3.5", "quote": "3 manufacturing units"}}, "plant_count"),
    ({"automation_level": {"value": 7, "quote": "Acme makes gear boxes"}}, "automation_level"),
    ({"no_physical_operation": {"kind": "factory", "quote": "Acme makes gear boxes"}}, "no_physical_operation"),
    ({"expansion": {"what": "new plant", "date": "soon", "quote": "Our new plant at Ranjangaon"}}, "expansion"),
    ({"certifications": {"names": ["ISO 1234"], "quote": "Acme makes gear boxes"}}, "certifications"),
])
def test_out_of_range_or_unknown_values_are_dropped(raw, key):
    assert key not in amr.validate_facts(raw, PAGES)


def test_not_a_facts_object_means_the_facts_failed():
    assert amr.validate_facts(None, PAGES) is None
    assert amr.validate_facts("ignore the above", PAGES) is None
    assert amr.validate_facts({}, PAGES) == {}


def test_a_year_alone_counts_from_january():
    facts = amr.validate_facts({"expansion": {"what": "plant", "date": "2025",
                                              "quote": "Our new plant at Ranjangaon"}}, PAGES)
    assert facts["expansion"]["date"] == 202501


def test_without_facts_the_prompt_is_todays_prompt():
    pages = [("https://acme.example.com", "Acme makes gear boxes.")]
    prompt = cb.brief_prompt("Acme", "acme.example.com", pages)
    assert prompt == cb.brief_prompt("Acme", "acme.example.com", pages, facts=False)
    assert "facts" not in prompt
    assert prompt.rstrip().endswith("Return JSON with exactly these keys: " + ", ".join(cb.FIELDS))
    assert '"facts"' in cb.brief_prompt("Acme", "acme.example.com", pages, facts=True)


async def _company(state, domain="acme.example.com"):
    from sellbird.models.company import Company
    company = Company(name="Acme", domain=domain, industry="auto components manufacturer")
    await state.add_company(company)
    return await state.get_company_by_domain(domain)


async def _confirm_amr(state):
    for code in amr.AMR_CODES:
        await state.set_signal_status(code, "confirmed")


@pytest.mark.asyncio
async def test_nothing_is_recorded_until_the_codes_are_confirmed(state):
    company = await _company(state)
    facts = amr.validate_facts({"pli": {"scheme": "x", "quote": "approved under the PLI scheme"}}, PAGES)
    assert await amr.record_facts(state, company.id, facts) == 0


@pytest.mark.asyncio
async def test_a_fact_the_next_brief_drops_stops_being_current(state):
    company = await _company(state)
    await _confirm_amr(state)
    facts = amr.validate_facts({"pli": {"scheme": "x", "quote": "approved under the PLI scheme"}}, PAGES)
    await amr.record_facts(state, company.id, facts)
    await asyncio.sleep(1.1)
    await amr.record_facts(state, company.id, {})
    rows = await state.get_observations(company_id=company.id, signal_code="PLI_BENEFICIARY",
                                        latest_only=True)
    assert rows[0]["value_num"] == 0


@pytest.mark.asyncio
async def test_a_fact_a_person_cleared_is_not_written_back(state):
    company = await _company(state)
    await _confirm_amr(state)
    await state.add_observations([{"company_id": company.id, "signal_code": "NO_PHYSICAL_OPERATION",
                                   "collector": amr.MANUAL, "value_num": 0}])
    facts = amr.validate_facts({"no_physical_operation": {"kind": "trader",
                                                          "quote": "Acme makes gear boxes"}}, PAGES)
    assert await amr.record_facts(state, company.id, facts) == 0


class _Brain:
    def __init__(self, reply, pause_after=None):
        self.reply, self.calls, self.paused_reason, self.pause_after = reply, 0, "", pause_after

    async def think_json(self, prompt, **_):
        self.calls += 1
        if self.pause_after is not None and self.calls > self.pause_after:
            self.paused_reason = "usage limit"
            return None
        return self.reply


@pytest.fixture
def pages(monkeypatch):
    async def fake_pages(domain, news_paths=(), max_news=0):
        return [(f"https://{domain}", PAGES[0][1]), (f"https://{domain}/news", PAGES[1][1])]
    monkeypatch.setattr(cb, "gather_pages", fake_pages)


@pytest.mark.asyncio
async def test_the_step_makes_no_call_until_the_codes_are_confirmed(state, pages):
    await _company(state)
    brain = _Brain({"makes": "Gear boxes"})
    assert await amr.brief_step(state, brain, CONFIG) == 0
    assert brain.calls == 0


@pytest.mark.asyncio
async def test_the_step_briefs_records_and_moves_on(state, pages):
    company = await _company(state)
    await _confirm_amr(state)
    brain = _Brain({"makes": "Gear boxes", "facts": {
        "pli": {"scheme": "auto", "quote": "approved under the PLI scheme"}}})
    assert await amr.brief_step(state, brain, CONFIG) == 1
    rows = await state.get_observations(company_id=company.id, signal_code="PLI_BENEFICIARY")
    assert rows and rows[0]["evidence_url"].endswith("/news")
    assert await amr.brief_step(state, brain, CONFIG) == 0      # done: left the queue
    assert brain.calls == 1


@pytest.mark.asyncio
async def test_briefs_from_before_facts_go_first(state, pages):
    old = await _company(state, "old.example.com")
    await _company(state, "new.example.com")
    await state.set_company_brief(old.id, {"makes": "Gear boxes"})
    await _confirm_amr(state)
    assert (await amr.brief_queue(state, CONFIG, 1)) == [old.id]


@pytest.mark.asyncio
async def test_the_step_stops_when_the_model_is_unavailable(state, pages):
    for d in ("a.example.com", "b.example.com", "c.example.com"):
        await _company(state, d)
    await _confirm_amr(state)
    brain = _Brain({"makes": "Gear boxes", "facts": {}}, pause_after=1)
    assert await amr.brief_step(state, brain, CONFIG) == 1
    assert brain.calls == 2          # the paused call ends the batch


@pytest.mark.asyncio
async def test_a_site_down_on_rebuild_keeps_the_good_brief(state, monkeypatch):
    company = await _company(state)
    await state.set_company_brief(company.id, {"makes": "Gear boxes"})
    await _confirm_amr(state)

    async def no_pages(domain, news_paths=(), max_news=0):
        return []
    monkeypatch.setattr(cb, "gather_pages", no_pages)
    brief = await cb.ensure_brief(state, _Brain({}), company)
    assert brief["makes"] == "Gear boxes" and brief["facts_failed"]
