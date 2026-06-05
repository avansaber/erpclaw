# Tests for erpclaw-journals

This sub-skill's actions are routed via foundation's `ROUTE_TO_SUBSKILL`
map in `source/erpclaw/scripts/db_query.py`. Coverage lives at the
foundation tier:

- **L2 contract:** `testing/integration/contract/test_erpclaw_contract.py`
  (3,085 generated tests cover every routed action).
- **L3 smoke:** `testing/integration/smoke/` foundation-domain scenarios.
- **L0 constitutional:** `testing/unit/constitution/` (action-completeness
  + GL invariants apply across all sub-skills).

Per the 2026-05-25 hygiene scan triage (and the closed
`internal/completed/2026/sprints/META_L2_CONTRACT_GAP_PLAN_2026-05-25.md`):
empty per-sub-skill `tests/` dirs are expected because functions named for
the *action* (`test_<action_name>`), not the sub-module, land in the
foundation contract test.

If you add sub-skill-only logic that's NOT routed via `ROUTE_TO_SUBSKILL`
(rare; would violate the foundation routing pattern), tests for it can
live in this directory.
