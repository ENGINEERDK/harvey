# TODOS

## Saved cohort lists in the dashboard
**What:** save named cohort-builder queries (require/exclude/ranges) and load them from the Signals tab.
**Why:** lists of companies by buying signal without retyping the query.
**Pros:** one click for recurring lists. **Cons:** new storage, UI, and a min-confidence filter in `state.cohort_companies` (upstream files).
**Context:** deferred in favour of `--industry`/`--signal` on `sellbird export` and `sellbird companies`.
**Depends on:** nothing.

## Tagged export for a CRM
**What:** a re-runnable companies/contacts export with `category:value` tags from a fixed vocabulary.
**Why:** import into a CRM and build lists there.
**Pros:** lists live where the team works. **Cons:** the tag shape depends on the CRM, not chosen yet.
**Context:** design it once a CRM is chosen; the AMR facts and fit band already appear in both exports.
**Depends on:** a CRM decision.
