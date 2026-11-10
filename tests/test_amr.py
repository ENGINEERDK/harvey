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


# ── Who gets written to ──

from sellbird.models.company import Company  # noqa: E402
from sellbird.models.prospect import Prospect  # noqa: E402

GATED = SimpleNamespace(
    icp=SimpleNamespace(industries=["auto components manufacturer"], titles=["Plant Head"],
                        hiring_signals=["forklift operator"], exclude_titles=["CFO", "HR"]),
    compliance=SimpleNamespace(do_not_contact=[]),
    channels=SimpleNamespace(email=SimpleNamespace(max_daily_sends=2, min_write_score=0)),
    writer=SimpleNamespace(company_brief=False))


async def _prospect(state, company, title, n):
    p = Prospect(company_id=company.id, first_name=f"P{n}", last_name="X",
                 email=f"p{n}@{company.domain}", email_status="verified", title=title,
                 company=company.name)
    p.id = await state.add_prospect(p)
    return p


def test_gate_reason():
    assert amr.gate_reason("CFO", "c1", GATED, set()).startswith("title not targeted")
    assert amr.gate_reason("Head of HR", "c1", GATED, set())
    assert amr.gate_reason("Plant Head", "c1", GATED, {"c1"}) == "no plant or warehouse of its own"
    assert amr.gate_reason("Plant Head", "c1", GATED, set()) == ""


@pytest.mark.asyncio
async def test_writer_and_count_agree_on_who_is_writable(state):
    good = await _company(state, "good.example.com")
    trader = await _company(state, "trader.example.com")
    await state.add_observations([{"company_id": trader.id, "signal_code": "NO_PHYSICAL_OPERATION",
                                   "collector": "amr_brief", "value_text": "trader", "confidence": 0.9}])
    ps = [await _prospect(state, good, "Plant Head", 1), await _prospect(state, good, "CFO", 2),
          await _prospect(state, trader, "Plant Head", 3)]
    chosen = await amr.select_for_writing(state, _Brain({}), GATED, ps)
    assert [p.first_name for p in chosen] == ["P1"]
    assert await state.count_writable_prospects(GATED) == 1
    assert await state.count_writable_prospects() == 3        # no config: the old count


@pytest.mark.asyncio
async def test_best_fit_first_and_no_more_than_three_days_of_sends(state):
    weak = await _company(state, "weak.example.com")
    weak_p = await _prospect(state, weak, "Plant Head", 1)
    strong = Company(name="Strong", domain="strong.example.com", industry="auto components manufacturer")
    await state.add_company(strong)
    strong = await state.get_company_by_domain("strong.example.com")
    await state.add_observations([{"company_id": strong.id, "signal_code": "PLI_BENEFICIARY",
                                   "collector": "amr_brief", "value_text": "auto", "confidence": 0.9}])
    ps = [weak_p] + [await _prospect(state, strong, "Plant Head", n) for n in range(2, 9)]
    chosen = await amr.select_for_writing(state, _Brain({}), GATED, ps)
    assert len(chosen) == 6                                  # 3 days x 2 a day
    assert weak_p not in chosen                              # the weaker company waits


@pytest.mark.asyncio
async def test_a_full_queue_drafts_nothing_and_counts_nothing(state, monkeypatch):
    good = await _company(state, "good.example.com")
    ps = [await _prospect(state, good, "Plant Head", 1)]

    async def full(_state):
        return 6
    monkeypatch.setattr(amr, "unsent_cold", full)
    assert await amr.select_for_writing(state, _Brain({}), GATED, ps) == []
    assert await state.count_writable_prospects(GATED) == 0


@pytest.mark.asyncio
async def test_with_codes_confirmed_the_company_is_briefed_before_drafting(state, monkeypatch):
    company = await _company(state, "trader.example.com")
    ps = [await _prospect(state, company, "Plant Head", 1)]
    await _confirm_amr(state)

    async def fake_pages(domain, news_paths=(), max_news=0):
        return [(f"https://{domain}", "We are a trading house for bearings and seals across India.")]
    monkeypatch.setattr(cb, "gather_pages", fake_pages)
    brain = _Brain({"makes": "Bearings", "facts": {"no_physical_operation": {
        "kind": "trader", "quote": "We are a trading house for bearings"}}})
    config = SimpleNamespace(**{**vars(GATED), "writer": SimpleNamespace(company_brief=True)})
    assert await amr.select_for_writing(state, brain, config, ps) == []
    assert brain.calls == 1


# ── sellbird amr check / clear ──

def _screen(tmp_path, rows):
    path = tmp_path / "screen.csv"
    path.write_text("domain,verdict,reason\n" + "".join(f"{d},{v},{r}\n" for d, v, r in rows))
    return str(path)


def test_read_screen_normalises_domains(tmp_path):
    path = _screen(tmp_path, [("https://www.acme.example.com/about", "Keep", "good"),
                              ("bad.example.com", "maybe", "skip me")])
    assert amr.read_screen(path) == [{"domain": "acme.example.com", "verdict": "keep",
                                      "reason": "good", "name": ""}]


class _ByDomain(_Brain):
    def __init__(self, blocked):
        super().__init__({})
        self.blocked = blocked
        self.domain = ""

    async def think_json(self, prompt, **_):
        self.calls += 1
        if any(d in prompt for d in self.blocked):
            return {"makes": "x", "facts": {"no_physical_operation": {
                "kind": "trader", "quote": "Acme makes gear boxes"}}}
        return {"makes": "x", "facts": {}}


@pytest.mark.asyncio
async def test_check_passes_when_no_good_plant_is_blocked(pages):
    screen = [{"domain": f"r{i}.example.com", "verdict": "reject", "reason": "", "name": ""}
              for i in range(5)] + [{"domain": "good.example.com", "verdict": "keep", "reason": "", "name": ""}]
    lines, passed = await amr.check_screen(_ByDomain(["r0.example.com"]), screen)
    assert passed and "PASS" in lines[-1]


@pytest.mark.asyncio
async def test_check_fails_on_a_wrong_block_or_too_few_rejects(pages):
    good = {"domain": "good.example.com", "verdict": "keep", "reason": "", "name": ""}
    rejects = [{"domain": f"r{i}.example.com", "verdict": "reject", "reason": "", "name": ""}
               for i in range(5)]
    assert not (await amr.check_screen(_ByDomain(["good.example.com"]), rejects + [good]))[1]
    assert not (await amr.check_screen(_ByDomain([]), rejects[:4] + [good]))[1]


@pytest.mark.asyncio
async def test_check_writes_nothing(state, pages):
    await _company(state)
    await _confirm_amr(state)
    before = len(await state.get_observations(limit=100000))
    await amr.check_screen(_ByDomain(["acme.example.com"]),
                           [{"domain": "acme.example.com", "verdict": "keep", "reason": "", "name": ""}])
    assert len(await state.get_observations(limit=100000)) == before
    assert (await state.get_company_brief((await state.get_company_by_domain("acme.example.com")).id))[0] == {}


@pytest.mark.asyncio
async def test_clear_lifts_a_wrong_block_for_good(state):
    company = await _company(state)
    await _confirm_amr(state)
    facts = {"no_physical_operation": {"kind": "trader", "quote": "q", "evidence_url": "https://a"}}
    await amr.record_facts(state, company.id, facts)
    assert company.id in await amr.no_floor_companies(state)
    await asyncio.sleep(1.1)
    assert await amr.clear_fact(state, "acme.example.com")
    assert company.id not in await amr.no_floor_companies(state)
    assert await amr.record_facts(state, company.id, facts) == 0
    with pytest.raises(ValueError):
        await amr.clear_fact(state, "acme.example.com", "HIRING_ROLE")


# ── Exports ──

from datetime import datetime, timezone  # noqa: E402

from tests.test_fit import fact  # noqa: E402

def test_present_codes_and_summary():
    now = datetime.now(timezone.utc)
    obs = [fact("EXPANSION_NEWS", now.year * 100 + now.month, "new plant"),
           fact("PLANT_COUNT", 3), fact("CERTIFICATION", text="IATF 16949"),
           fact("PLI_BENEFICIARY", text="x", conf=0.5)]
    assert amr.present_codes(obs) == ["EXPANSION_NEWS", "PLANT_COUNT", "CERTIFICATION"]
    summary = amr.facts_summary(obs)
    assert "expanding: new plant" in summary and "3 plants" in summary and "IATF 16949" in summary
    assert "PLI" not in summary


def test_matches_filters():
    row = {"sector": "electronics manufacturing company", "signal_codes": ["EXPANSION_NEWS"]}
    assert amr.matches_filters(row, ["Electronics"], None)
    assert not amr.matches_filters(row, ["pharma"], None)
    assert amr.matches_filters(row, None, ["expansion_news"])
    assert not amr.matches_filters(row, ["electronics"], ["EXPANSION_NEWS", "PLI_BENEFICIARY"])


@pytest.mark.asyncio
async def test_export_filters_by_industry_and_status(state):
    from sellbird.export import collect_prospects
    elec = await _company(state, "elec.example.com")
    async with state._connect() as db:
        await db.execute("UPDATE companies SET industry = 'electronics manufacturing company' "
                         "WHERE id = ?", (elec.id,))
        await db.commit()
    pharma = await _company(state, "pharma.example.com")
    a = await _prospect(state, elec, "Plant Head", 1)
    await _prospect(state, pharma, "Plant Head", 2)
    await state.update_prospect_status(a.id, "replied")
    rows = await collect_prospects(state, statuses=["replied"], config=GATED,
                                   industries=["electronics"])
    assert [r["first_name"] for r in rows] == ["P1"]
    assert rows[0]["fit_band"] in ("A", "B", "C")
