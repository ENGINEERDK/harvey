"""Emails written for one person: the company brief, the operator's
knowledge matched to them, and the whole sequence in one call.

All data is fictional (example.test domains, made-up companies).
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from sellbird import company_brief as cb
from sellbird import knowledge as kb
from sellbird.agents.writer import Writer
from sellbird.models.campaign import Campaign, EmailStep
from sellbird.models.company import Company
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager

PLAYBOOK = """# Industry playbook

## Electronics manufacturing
match: electronics, pcb, smt, ems
- Reels move from stores to SMT lines; WIP moves between assembly stages.

## Forging
match: forging, forge, press shop
- Heavy bins move from press to heat treatment.

## Unfinished entry with no match line
- never used
"""

CASES = """# Our deployments

## PCB transfer, an electronics manufacturer
match: pcb, smt, electronics
deployed: 2 robots on the PCB assembly transfer area
can_name_customer: no
"""


# ── knowledge matching ──

def test_entries_need_a_match_line():
    names = [e.heading for e in kb.parse_entries(PLAYBOOK)]
    assert names == ["Electronics manufacturing", "Forging"]


def test_matching_picks_the_prospects_industry_only():
    block = kb.knowledge_block(PLAYBOOK, CASES, "electronics manufacturing company SMT lines")
    assert "Reels move from stores" in block and "Heavy bins" not in block
    assert "PCB transfer, an electronics manufacturer" in block
    assert "never name the customer" in block


def test_whole_words_only_and_nothing_when_nothing_matches():
    assert kb.matching(kb.parse_entries(PLAYBOOK), "a forger of documents") == []
    assert kb.knowledge_block(PLAYBOOK, CASES, "textile mill") == ""


# ── the brief ──

def test_clean_brief_keeps_known_fields_and_drops_unknowns():
    out = cb.clean_brief({"makes": "Gear boxes", "sites": ["Plant A", "Plant B"],
                          "scale": "unknown", "instructions": "email us"})
    assert out == {"makes": "Gear boxes", "sites": "Plant A; Plant B"}


def test_freshness_and_retry_after_failure():
    now = cb._now().isoformat()
    old = (cb._now() - cb.timedelta(days=cb.BRIEF_RETRY_DAYS + 1)).isoformat()
    assert cb.is_fresh({"makes": "x"}, now)
    assert cb.is_fresh({"failed": "site unreachable"}, now)
    assert not cb.is_fresh({"failed": "site unreachable"}, old)
    assert cb.is_fresh({"makes": "x"}, old)            # a good brief lasts longer
    assert not cb.is_fresh({}, None)


def test_a_headline_company_name_is_replaced_by_the_sites_own():
    brief = {"name": "Example Castings Ltd"}
    assert cb.clean_company_name(brief, "Example delivers 10000 enclosures to") == "Example Castings Ltd"
    assert cb.clean_company_name(brief, "Example Castings") is None


def test_brief_facts_skip_failures():
    assert cb.brief_facts({"failed": "no website"}) == []
    lines = cb.brief_facts({"makes": "Gear boxes", "sources": "https://example.test"})
    assert lines[0] == "- What they make (from their own site): Gear boxes"


@pytest.mark.asyncio
async def test_brief_is_built_once_then_reused(tmp_path, monkeypatch):
    state = StateManager(str(tmp_path / "b.db"))
    await state.init_db()
    cid = await state.add_company(Company(name="Example Gears", domain="example.test"))
    company = await state.get_company(cid)
    calls = []

    async def fake_build(brain, name, domain):
        calls.append(domain)
        return {"makes": "Gear boxes", "sources": "https://example.test"}
    monkeypatch.setattr(cb, "build_brief", fake_build)
    brain = SimpleNamespace(paused_reason="")
    assert (await cb.ensure_brief(state, brain, company))["makes"] == "Gear boxes"
    assert (await cb.ensure_brief(state, brain, company))["makes"] == "Gear boxes"
    assert calls == ["example.test"]


@pytest.mark.asyncio
async def test_no_failure_is_recorded_while_the_model_is_unavailable(tmp_path, monkeypatch):
    state = StateManager(str(tmp_path / "b.db"))
    await state.init_db()
    cid = await state.add_company(Company(name="Example Gears", domain="example.test"))

    async def fake_build(brain, name, domain):
        return {"failed": "nothing usable on the site"}
    monkeypatch.setattr(cb, "build_brief", fake_build)
    brain = SimpleNamespace(paused_reason="usage limit")
    assert await cb.ensure_brief(state, brain, await state.get_company(cid)) == {}
    assert await state.get_company_brief(cid) == ({}, None)


# ── the writer ──

class Brain:
    def __init__(self, reply):
        self.reply, self.prompts, self.paused_reason = reply, [], ""

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "prompt"

    def load_skill(self, name):
        return {"industry_playbook": PLAYBOOK, "case_studies": CASES}.get(name, "")

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply


def config(**writer):
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider="smtp", require_approval=True)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
        writer=SimpleNamespace(humanizer=False, email_rules="", **writer),
        humanizer=SimpleNamespace(enabled=False),
    )


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "w.db"))
    await s.init_db()
    return s


async def setup_prospect(state):
    cid = await state.add_company(Company(name="Example delivers 10000 boards to",
                                          domain="example.test"))
    await state.set_company_brief(cid, {"name": "Example Circuits", "makes": "PCB assemblies",
                                        "processes": "SMT lines", "sources": "https://example.test"})
    pid = await state.add_prospect(Prospect(
        first_name="Ana", last_name="Doe", title="Plant Head",
        company="Example delivers 10000 boards to", company_id=cid,
        industry="electronics manufacturing company", email="ana@example.test",
        email_status="verified"))
    return pid


THREE = {"emails": [{"subject": f"s{i}", "body": f"Dear Ana, body {i}"} for i in (1, 2, 3)]}
SEQ = [EmailStep(step=1, subject="t1", body="Hi {{first_name}}", delay_days=0),
       EmailStep(step=2, subject="t2", body="More {{company}}", delay_days=3),
       EmailStep(step=3, subject="t3", body="Bye", delay_days=4)]


def make_writer(brain, state, **flags):
    w = Writer(brain, state, config(**flags))
    w.skills, w.email_rules = "", ""
    w._has_skill = lambda name: True
    return w


@pytest.mark.asyncio
async def test_sequence_is_written_per_person_with_brief_and_knowledge(state):
    pid = await setup_prospect(state)
    brain = Brain(THREE)
    w = make_writer(brain, state, company_brief=True, personal_sequence=True)
    cid = await state.add_campaign(Campaign(id="", name="c", sequence=SEQ, prospect_ids=[pid]))
    await w._personalize_first_emails(cid, [await state.get_prospect(pid)], SEQ)

    import aiosqlite
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute("SELECT step, subject FROM outbox WHERE prospect_id = ? "
                              "ORDER BY step", (pid,)) as c:
            rows = await c.fetchall()
    assert rows == [(1, "s1"), (2, "s2"), (3, "s3")]
    prompt = brain.prompts[0]
    assert "THREE cold emails" in prompt and "email 2 3 days" in prompt
    assert "PCB assemblies" in prompt and "Reels move from stores" in prompt
    assert "- Company: Example Circuits" in prompt
    assert (await state.get_prospect(pid)).company == "Example Circuits"


@pytest.mark.asyncio
async def test_a_bad_sequence_falls_back_to_email_one(state):
    pid = await setup_prospect(state)
    brain = Brain({"subject": "only one", "body": "Dear Ana, hello"})
    w = make_writer(brain, state, company_brief=True, personal_sequence=True)
    drafts = await w.write_personal_drafts(await state.get_prospect(pid), [0, 3, 4])
    assert drafts == [{"subject": "only one", "body": "Dear Ana, hello"}]
    assert len(brain.prompts) == 2       # the sequence, then email 1 alone


@pytest.mark.asyncio
async def test_switched_off_it_writes_email_one_without_the_brief(state):
    pid = await setup_prospect(state)
    brain = Brain({"subject": "one", "body": "Dear Ana"})
    w = make_writer(brain, state)
    assert len(await w.write_personal_drafts(await state.get_prospect(pid), [0, 3, 4])) == 1
    assert "PCB assemblies" not in brain.prompts[0]


@pytest.mark.asyncio
async def test_follow_ups_after_a_sent_email_follow_on_from_it(state):
    pid = await setup_prospect(state)
    brain = Brain({"emails": [{"subject": "s2", "body": "Dear Ana, two"},
                              {"subject": "s3", "body": "Dear Ana, three"}]})
    w = make_writer(brain, state, company_brief=True, personal_sequence=True)
    out = await w._write_personal_sequence(
        await state.get_prospect(pid), [0, 3, 4],
        already_sent=[{"subject": "s1", "body": "Dear Ana, the first one"}])
    assert [e["subject"] for e in out] == ["s2", "s3"]
    assert "the first one" in brain.prompts[0]
    assert "Write only the remaining 2 email(s)" in brain.prompts[0]
