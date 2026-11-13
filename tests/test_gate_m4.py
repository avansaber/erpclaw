"""The literal public oracle, mutation coverage, transport precedence and qualification controls."""
from __future__ import annotations

import copy
import json
import re
from uuid import UUID
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from erpclaw_cli.evals import gate, gate_public
from erpclaw_cli.evals.gate_oracle import (Matcher, Mismatch, ORIGIN, PATTERNS, PLACEHOLDER, PREFIX,
                                         compare_capture, placeholder_fixture, premature)
from erpclaw_cli.evals.gate_public import CASES, check_public, changes

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/gate_m4"
INVOICE = gate.load_fixture(FIXTURES, CASES[0])
FENCE_BULLET = (
    "The conversation's approval fence is the one control row any turn may change, question turns included: "
    "the conversation has exactly one conversation_fence row, its generation never decreases, "
    "its live token is opaque and is never compared to a figure in a key, "
    "and its values are as the engine's own fence tests specify. "
    "No other control row changes on a question turn."
)


@pytest.fixture
def base_conventions(tmp_path):
    path = tmp_path / 'conventions_without_fence_bullet.md'
    path.write_bytes((FIXTURES / path.name).read_bytes())
    return path


@pytest.fixture
def fence_conventions(tmp_path):
    path = tmp_path / 'conventions_with_fence_bullet.md'
    path.write_bytes((FIXTURES / path.name).read_bytes())
    return path


def test_pinned_conventions_match_live_and_differ_only_by_the_fence_bullet():
    without_bullet = (FIXTURES / 'conventions_without_fence_bullet.md').read_bytes()
    with_bullet = (FIXTURES / 'conventions_with_fence_bullet.md').read_bytes()
    assert with_bullet == (ROOT / 'tests/expected_books/CONVENTIONS.md').read_bytes()
    bullet = (
        b"- **The conversation's approval fence** is the one control row any turn may change, question turns\n"
        b"  included: the conversation has exactly one conversation_fence row, its generation never decreases,\n"
        b"  its live token is opaque and is never compared to a figure in a key, and its values are as the\n"
        b"  engine's own fence tests specify. No other control row changes on a question turn.\n"
    )
    assert ' '.join(bullet.decode().removeprefix('- ').replace('**', '').split()) == FENCE_BULLET
    assert with_bullet.count(bullet) == 1
    assert with_bullet.replace(bullet, b'', 1) == without_bullet


def fixture(frozen=INVOICE):
    """An independent synthetic capture with concrete values for the frozen bindings."""
    def generated(match):
        kind, number = match[1], int(match[2])
        if kind == 'uuid':
            return str(UUID(int=number))
        return format(number + 1000, '016x') * (int(kind[3:]) // 16)

    def expand(value):
        if isinstance(value, dict):
            return {k: expand(v) for k, v in value.items()}
        if isinstance(value, list):
            return [expand(v) for v in value]
        if isinstance(value, str):
            value = PREFIX.sub(lambda m: generated(PLACEHOLDER.fullmatch(m[1] + '>'))[:16], value)
            return PLACEHOLDER.sub(generated, value).replace(ORIGIN, '0' * 64)
        return value

    return expand(frozen)


def test_captured_invoice_matches_placeholders_and_fence_finding_depends_on_bullet(
        base_conventions, fence_conventions):
    assert compare_capture(INVOICE, fixture()) is None
    assert placeholder_fixture(fixture()) == INVOICE
    assert INVOICE['public_keys'] == gate.public_keys(ROOT)
    findings = check_public(CASES[0], INVOICE, base_conventions)
    assert findings and all(
        f['key'] == 'CONVENTIONS.md:13-19' and 'conversation_fence changes' in f['rule'] for f in findings
    ), 'm-check-exempts-fence-regardless'
    assert check_public(CASES[0], INVOICE, fence_conventions) == [], 'm-check-ignores-fence-bullet'
    assert check_public(CASES[0], INVOICE) == [], 'm-check-ignores-live-fence-bullet'


def question_capture():
    captured = fixture()
    captured['steps'] = [captured['steps'][0]]
    captured['steps'][0]['events'] = [{'type': 'ask', 'text': 'Which customer is ABC?'}]
    captured['after'] = copy.deepcopy(captured['steps'][0]['books'])
    return captured


@pytest.mark.parametrize('case', (CASES[0], CASES[3]), ids=('invoice', 'question'))
def test_base_conventions_reject_fence_changes_as_s1_findings(base_conventions, case):
    captured = fixture() if case.operation else question_capture()
    # Captured metadata is not authority for the conventions in this build.
    captured['public_keys']['CONVENTIONS.md'] += '\n- ' + FENCE_BULLET + '\n'
    findings = check_public(case, captured, base_conventions)
    assert findings and all(f['key'].startswith('CONVENTIONS.md:') and
                            'conversation_fence changes' in f['rule'] for f in findings)


@pytest.mark.parametrize('mutant', ('m-remove-convention-fence-check',))
def test_removing_convention_check_wrongly_accepts_base_invoice(base_conventions, monkeypatch, mutant):
    assert check_public(CASES[0], fixture(), base_conventions)
    monkeypatch.setattr(gate_public, 'check_turns', lambda *args, **kwargs: [])
    assert check_public(CASES[0], fixture(), base_conventions) == [], mutant


@pytest.mark.parametrize('mutant', ('m-base-fence-restored-after-question', 'm-base-fence-duplicate-existing-row'))
def test_base_fence_changes_cannot_hide_in_row_membership_or_final_snapshot(base_conventions, mutant):
    captured = question_capture()
    row = copy.deepcopy(captured['after']['conversation_fence'][0])
    if mutant == 'm-base-fence-restored-after-question':
        captured['after']['conversation_fence'] = []
    else:
        captured['before']['conversation_fence'] = [row]
        captured['steps'][0]['books']['conversation_fence'].append(copy.deepcopy(row))
    assert any('conversation_fence changes' in f.get('rule', '')
               for f in check_public(CASES[3], captured, base_conventions)), mutant


@pytest.mark.parametrize('mutant', ('m-near-match-fence-convention', 'm-wrapped-fence-convention'))
def test_only_the_exact_fence_bullet_authorizes_changes(base_conventions, mutant):
    # An unindented wrap is outside the list item.
    bullet = FENCE_BULLET.replace('never decreases', 'usually increases') if mutant.startswith('m-near') else (
        FENCE_BULLET.replace('question turns included: ', 'question turns included:\n'))
    base_conventions.write_text(base_conventions.read_text() + '\n- ' + bullet + '\n')
    assert check_public(CASES[0], fixture(), base_conventions)


@pytest.mark.parametrize('mutant, bullet', (
    pytest.param('m-fence-convention-one-word-change', '- ' + FENCE_BULLET.replace('never', 'sometimes', 1),
                 id='m-fence-convention-one-word-change'),
    pytest.param('m-fence-convention-dropped-sentence', '- ' + FENCE_BULLET.removesuffix(
        ' No other control row changes on a question turn.'), id='m-fence-convention-dropped-sentence'),
    pytest.param('m-fence-convention-split-items', '- ' + FENCE_BULLET.replace('. No other', '.\n- No other'),
                 id='m-fence-convention-split-items'),
    pytest.param('m-fence-convention-inside-another-bullet', '- Another rule: ' + FENCE_BULLET + ' End of rule.',
                 id='m-fence-convention-inside-another-bullet'),
))
def test_fence_convention_mutants_remain_s1_findings(base_conventions, mutant, bullet):
    base_conventions.write_text(base_conventions.read_text() + '\n' + bullet + '\n')
    findings = check_public(CASES[0], INVOICE, base_conventions)
    assert findings and all(
        f['key'] == 'CONVENTIONS.md:13-19'
        and f.get('rule') == 'conversation_fence changes require the exact fence convention'
        for f in findings
    ), mutant


@pytest.mark.parametrize('bullet', (
    '- ' + FENCE_BULLET,
    '- **' + FENCE_BULLET.replace('approval fence', 'approval fence**', 1).replace(
        'question turns included: ', 'question turns included:\n  '),
    '- ' + FENCE_BULLET.replace(' ', ' \t ').replace('included:', 'included:\n\t'),
), ids=('plain', 'bold-wrapped', 'whitespace'))
def test_fence_convention_normalizes_only_emphasis_and_whitespace(base_conventions, bullet):
    base_conventions.write_text(base_conventions.read_text() + '\n' + bullet + '\n')
    assert check_public(CASES[0], INVOICE, base_conventions) == []


@pytest.mark.parametrize('bullet', (
    '- ' + FENCE_BULLET.lower(),
    '- ' + FENCE_BULLET.removesuffix('.'),
    '- *' + FENCE_BULLET + '*',
    '- __' + FENCE_BULLET + '__',
    '- ' + FENCE_BULLET.replace('. No other', '.\n\n  No other'),
    FENCE_BULLET,
), ids=('case', 'punctuation', 'single-star', 'underscore', 'blank-line', 'no-list-item'))
def test_fence_convention_does_not_normalize_other_differences(base_conventions, bullet):
    base_conventions.write_text(base_conventions.read_text() + '\n' + bullet + '\n')
    assert any(f.get('rule') == 'conversation_fence changes require the exact fence convention'
               for f in check_public(CASES[0], INVOICE, base_conventions))


def test_fence_finding_names_the_items_first_line(fence_conventions):
    captured = question_capture()
    captured['steps'][0]['books']['conversation_fence'][0]['generation'] = '0'
    first_line = next(i for i, line in enumerate(fence_conventions.read_text().splitlines(), 1)
                      if line.startswith("- **The conversation's approval fence**"))
    findings = check_public(CASES[3], captured, fence_conventions)
    assert findings and all(f['key'] == f'CONVENTIONS.md:{first_line}' for f in findings)
    assert any(f.get('rule') == 'positive fence generation' for f in findings)


@pytest.mark.parametrize('generation', ('1', '7'))
def test_exact_fence_convention_accepts_equal_or_increased_generation(fence_conventions, generation):
    captured = question_capture()
    row = copy.deepcopy(captured['after']['conversation_fence'][0])
    captured['before']['conversation_fence'] = [row]
    for books in (captured['steps'][0]['books'], captured['after']):
        books['conversation_fence'][0]['generation'] = generation
    assert check_public(CASES[3], captured, fence_conventions) == []


def test_exact_fence_convention_accepts_invoice_without_comparing_token_figures(fence_conventions):
    captured = fixture()
    token = captured['after']['conversation_fence'][0]['live_token']
    captured = json.loads(json.dumps(captured).replace(token, 'e' * 32))
    assert check_public(CASES[0], captured, fence_conventions) == []
    assert check_public(CASES[0], INVOICE, fence_conventions) == []
    assert compare_capture(INVOICE, captured) is None


def test_fence_can_end_its_request_at_the_same_generation(fence_conventions):
    captured = question_capture()
    captured['before']['conversation_fence'] = copy.deepcopy(captured['after']['conversation_fence'])
    for books in (captured['steps'][0]['books'], captured['after']):
        books['conversation_fence'][0]['live_token'] = None
    assert check_public(CASES[3], captured, fence_conventions) == []


def test_an_unchanged_fence_keeps_its_seed_timestamp(fence_conventions):
    captured = question_capture()
    row = captured['steps'][0]['books']['conversation_fence'][0]
    row['updated_at'] = captured['window'][0]
    captured['before']['conversation_fence'] = [copy.deepcopy(row)]
    captured['after']['conversation_fence'] = [copy.deepcopy(row)]
    assert check_public(CASES[3], captured, fence_conventions) == []


@pytest.mark.parametrize('mutant', ('m-fence-changes-other-conversation',))
def test_fence_convention_preserves_other_conversations(fence_conventions, mutant):
    captured = fixture()
    other = {**captured['steps'][0]['books']['conversation_fence'][0], 'conversation_id': str(UUID(int=900))}
    for books in (captured['before'], *(s['books'] for s in captured['steps']), captured['after']):
        books['conversation_fence'].append(copy.deepcopy(other))
    assert check_public(CASES[0], captured, fence_conventions) == []
    captured['steps'][0]['books']['conversation_fence'][-1]['generation'] = '99'
    assert check_public(CASES[0], captured, fence_conventions), mutant


@pytest.mark.parametrize('mutant', ('m-fence-approval-token-reference',))
def test_fence_token_is_bound_to_the_approval_reference(fence_conventions, mutant):
    captured = fixture()
    captured['steps'][1]['books']['approval'][-1]['request_card_token'] = 'e' * 32
    assert any(f.get('rule') == 'approval refers to the opaque live token'
               for f in check_public(CASES[0], captured, fence_conventions)), mutant


@pytest.mark.parametrize('mutant', (
    'm-fence-zero-rows', 'm-fence-two-rows', 'm-fence-other-conversation', 'm-fence-generation-decrease',
    'm-fence-generation-invalid', 'm-fence-token-shape', 'm-fence-company', 'm-fence-timestamp',
    'm-fence-token-change-at-equal-generation', 'm-fence-intermediate-two-rows',
    'm-fence-intermediate-generation-decrease',
))
def test_exact_fence_convention_rejects_invalid_turns(fence_conventions, mutant):
    captured = question_capture()
    old = copy.deepcopy(captured['after']['conversation_fence'][0])
    captured['before']['conversation_fence'] = [old]
    for books in (captured['steps'][0]['books'], captured['after']):
        rows = books['conversation_fence']
        if mutant == 'm-fence-zero-rows':
            rows.clear()
        elif mutant in {'m-fence-two-rows', 'm-fence-intermediate-two-rows'}:
            rows.append(copy.deepcopy(rows[0]))
        elif mutant == 'm-fence-other-conversation':
            rows.append({**rows[0], 'conversation_id': str(UUID(int=900))})
        elif mutant in {'m-fence-generation-decrease', 'm-fence-intermediate-generation-decrease'}:
            old['generation'] = '9'
        elif mutant == 'm-fence-generation-invalid':
            rows[0]['generation'] = 'not-an-integer'
        elif mutant == 'm-fence-token-shape':
            rows[0]['live_token'] = 'not-a-token'
        elif mutant == 'm-fence-company':
            rows[0]['company_id'] = str(UUID(int=900))
        elif mutant == 'm-fence-timestamp':
            rows[0]['updated_at'] = '2099-01-01 00:00:00+00:00'
        elif mutant == 'm-fence-token-change-at-equal-generation':
            rows[0]['live_token'] = 'e' * 32
    if 'intermediate' in mutant:
        captured['after']['conversation_fence'] = [copy.deepcopy(old)]
    findings = check_public(CASES[3], captured, fence_conventions, conversation_id=old['conversation_id'])
    assert findings and any(f['key'].startswith('CONVENTIONS.md:') for f in findings), mutant


@pytest.mark.parametrize('table', ('approval', 'operation_outcome', 'audit_log', 'number_series',
                                  'posting_chain_head', 'principal', 'plugin_install'),
                         ids=lambda t: f'm-question-changes-{t}')
def test_question_changes_no_other_control_row_even_if_restored(fence_conventions, table):
    captured = question_capture()
    rows = captured['steps'][0]['books'][table]
    if rows:
        rows[0][next(iter(rows[0]))] = 'changed-control'
    else:
        rows.append({'id': 'extra-control-row'})
    # The final snapshot is clean: the intermediate question must still be checked.
    findings = check_public(CASES[3], captured, fence_conventions)
    assert any(f.get('rule') == 'question changes no other row, business or control' for f in findings)


@pytest.mark.parametrize('mutant', ('m-approved-extra-audit', 'm-approved-other-control-row'))
def test_fence_convention_keeps_the_other_approved_control_rules(fence_conventions, mutant):
    captured = fixture()
    if mutant == 'm-approved-extra-audit':
        captured['after']['audit_log'].append(copy.deepcopy(captured['after']['audit_log'][-1]))
    else:
        captured['after']['principal'][0]['name'] = 'changed-control'
    assert check_public(CASES[0], captured, fence_conventions), mutant


def test_premature_fence_write_requires_prior_convention_validation():
    captured = question_capture()
    assert premature(captured['before'], captured['after']) == 'premature write: conversation_fence'


@pytest.mark.parametrize('location', ('uuid', 'hex32', 'hex64', 'event', 'json', 'prefix'))
def test_fixture_loader_rejects_literal_generated_values(tmp_path, location):
    frozen = copy.deepcopy(INVOICE)
    actual = fixture()
    if location == 'event':
        frozen['steps'][1]['events'][0]['approval_id'] = actual['steps'][1]['events'][0]['approval_id']
    elif location in {'json', 'prefix'}:
        column = 'read_set_json' if location == 'json' else 'impl_version'
        frozen['after']['approval'][-1][column] = actual['after']['approval'][-1][column]
    else:
        for rows in frozen['before'].values():
            for row in rows:
                for column, value in row.items():
                    if isinstance(value, str) and value.startswith(f'<{location}:'):
                        row[column] = 'a' * 32 if location == 'hex32' else 'b' * 64 if location == 'hex64' else str(UUID(int=1))
                        break
                else:
                    continue
                break
            else:
                continue
            break
        else:
            # Request nonces appear only after the approval card.
            frozen['after']['approval'][0]['nonce'] = 'a' * 32
    (tmp_path / f'{CASES[0].id}.json').write_text(json.dumps(frozen))
    with pytest.raises(ValueError, match='literal generated|placeholder|prefix'):
        gate.load_fixture(tmp_path, CASES[0])


@pytest.mark.parametrize('kind', tuple(PATTERNS))
def test_distinct_placeholders_cannot_alias_and_references_cannot_split(kind):
    matcher = Matcher({}, INVOICE['window'])
    first = str(UUID(int=1)) if kind == 'uuid' else '1' * int(kind[3:])
    other = str(UUID(int=2)) if kind == 'uuid' else '2' * int(kind[3:])
    matcher.text(f'<{kind}:1>', first, 'first')
    with pytest.raises(Mismatch, match='alias'):
        matcher.text(f'<{kind}:2>', first, 'second')
    with pytest.raises(Mismatch, match='broken generated-value reference'):
        matcher.text(f'<{kind}:1>', other, 'repeated')


def test_fixture_contains_no_long_hex_or_base64_like_runs():
    for path in FIXTURES.glob('*.json'):
        text = re.sub(r'<[^>]*>', '', path.read_text())
        assert re.search(r'[A-Za-z0-9+/]{20,}', text) is None


def test_fixture_loader_rejects_skipped_binding_numbers(tmp_path):
    frozen = json.dumps(INVOICE).replace('<uuid:1>', '<uuid:999>')
    (tmp_path / f'{CASES[0].id}.json').write_text(frozen)
    with pytest.raises(ValueError, match='first-appearance order'):
        gate.load_fixture(tmp_path, CASES[0])


def test_capture_aliasing_distinct_uuid_placeholders_fails():
    actual = fixture()
    first, second = str(UUID(int=1)), str(UUID(int=2))
    aliased = json.loads(json.dumps(actual).replace(second, first))
    assert 'alias' in compare_capture(INVOICE, aliased)


def test_bound_text_keeps_literal_case_and_token_width():
    matcher = Matcher({}, INVOICE['window'])
    with pytest.raises(Mismatch, match='text differs'):
        matcher.text('Invoice <hex32:1>', 'invoice ' + 'a' * 32, 'card')
    with pytest.raises(Mismatch, match='text differs'):
        matcher.text('<hex32:1>', 'a' * 64, 'token')


# Enumerate independently: deleting a fixture table or comparison must not silently shrink the matrix.
TOUCHED = ("approval", "audit_log", "conversation_fence", "ledger_entry", "number_series", "operation_outcome",
           "posting", "posting_chain_head", "sales_invoice", "sales_invoice_issue", "sales_invoice_line",
           "stock_head", "stock_ledger_entry")


def test_mutation_matrix_covers_every_touched_table():
    assert set(changes(INVOICE['before'], INVOICE['after'])) == set(TOUCHED)


@pytest.mark.parametrize("table", TOUCHED, ids=lambda t: f"m-{t}-wrong-value")
def test_each_touched_table_wrong_value_fails(table):
    mutated = fixture()
    row = mutated['after'][table][-1]
    literals = [c for c, v in row.items() if INVOICE['rules'][f'{table}.{c}'] == 'literal' and v is not None]
    column = literals[0] if literals else next(iter(row))
    row[column] = 'wrong-value'
    assert table in compare_capture(INVOICE, mutated)


@pytest.mark.parametrize("table", TOUCHED, ids=lambda t: f"m-{t}-missing-row")
def test_each_touched_table_missing_row_fails(table):
    mutated = fixture()
    mutated['after'][table].pop()
    assert table in compare_capture(INVOICE, mutated)


@pytest.mark.parametrize("table", TOUCHED, ids=lambda t: f"m-{t}-extra-row")
def test_each_touched_table_extra_row_fails(table):
    mutated = fixture()
    mutated['after'][table].append(copy.deepcopy(mutated['after'][table][-1]))
    assert table in compare_capture(INVOICE, mutated)


@pytest.mark.parametrize("table", tuple(INVOICE['before']), ids=lambda t: f"m-seed-{t}-extra-row")
def test_no_seed_table_is_ignored_even_when_empty(table):
    mutated = fixture()
    mutated['before'][table].append(copy.deepcopy(mutated['before'][table][0]) if mutated['before'][table] else {})
    assert table in compare_capture(INVOICE, mutated)


@pytest.mark.parametrize("table", tuple(t for t, rows in INVOICE['before'].items() if rows),
                         ids=lambda t: f"m-seed-{t}-missing-row")
def test_every_populated_seed_table_is_compared(table):
    mutated = fixture()
    mutated['before'][table].pop()
    assert table in compare_capture(INVOICE, mutated)


@pytest.mark.parametrize("mutant", ("m-wrong-question", "m-free-text", "m-wrong-card-string", "m-card-on-ask",
                                     "m-fence-missing", "m-fence-generation", "m-fence-token", "m-control-status",
                                     "m-broken-reference", "m-business-date", "m-early-write"))
def test_events_fence_controls_and_premature_writes(mutant):
    mutated = fixture()
    if mutant == "m-wrong-question":
        mutated['steps'][0]['events'][0]['code'] = 'customer_unknown'
    elif mutant == "m-free-text":
        mutated['steps'][0]['events'][0]['type'] = 'answer'
    elif mutant == "m-wrong-card-string":
        mutated['steps'][1]['events'][0]['card']['lines'][2][1] = '5 x Copper Wire = 140.01'
    elif mutant == "m-card-on-ask":
        mutated['steps'][0]['events'].append(mutated['steps'][1]['events'][0])
    elif mutant == "m-fence-missing":
        mutated['after']['conversation_fence'] = []
    elif mutant == "m-fence-generation":
        mutated['after']['conversation_fence'][0]['generation'] = '1'
    elif mutant == "m-fence-token":
        mutated['after']['conversation_fence'][0]['live_token'] = '1' * 32
    elif mutant == "m-control-status":
        mutated['after']['approval'][-1]['status'] = 'pending'
    elif mutant == "m-broken-reference":
        mutated['after']['sales_invoice_line'][-1]['invoice_id'] = '00000000-0000-0000-0000-000000000000'
    elif mutant == "m-business-date":
        mutated['after']['stock_head'][0]['last_date'] = '2026-10-25'
    elif mutant == "m-early-write":
        mutated['steps'][0]['books']['sales_invoice'].append(mutated['after']['sales_invoice'][-1])
        business_write = {**mutated['steps'][0]['books'], 'conversation_fence': mutated['before']['conversation_fence']}
        assert premature(mutated['before'], business_write) == 'premature write: sales_invoice'
    assert compare_capture(INVOICE, mutated)


def test_same_shape_wrong_ledger_amount_is_not_a_count_or_shape_check():
    mutated = fixture()
    mutated['after']['ledger_entry'][-1]['credit'] = '140.01'
    assert 'ledger_entry' in compare_capture(INVOICE, mutated)


def test_seed_rate_and_customer_mutations_are_rejected():
    mutated = fixture()
    mutated['before']['item'][0]['standard_rate'] = '28.01'
    assert 'item.standard_rate' in compare_capture(INVOICE, mutated)
    mutated = fixture()
    mutated['before']['party'].append({**mutated['before']['party'][0], 'name': 'Extra Customer'})
    assert 'party' in compare_capture(INVOICE, mutated)


def test_timestamps_have_a_window_and_business_dates_are_literal():
    mutated = fixture()
    mutated['after']['approval'][-1]['created_at'] = '2099-01-01 00:00:00+00:00'
    assert 'timestamp' in compare_capture(INVOICE, mutated)


def result_set():
    return {c.id: [gate.Trial()] for c in CASES}


def test_one_trial_required_for_each_of_exactly_five_cases():
    results = result_set()
    assert gate.aggregate(results) == 'PASS'
    results[CASES[0].id][0].fail('wrong amount')
    assert gate.aggregate(results) == 'FAIL'
    results = result_set()
    results['extra'] = [gate.Trial()]
    assert gate.aggregate(results) == 'FAIL'
    results = result_set()
    results[CASES[0].id].pop()
    assert gate.aggregate(results) == 'FAIL'
    results = result_set()
    results[CASES[0].id].append(gate.Trial())
    assert gate.aggregate(results) == 'FAIL'


@pytest.mark.parametrize("also_fail", (False, True))
def test_any_inconclusive_makes_the_run_inconclusive(also_fail):
    results = result_set()
    results[CASES[0].id][0].inconclusive('model_unavailable statuses [504]')
    if also_fail:
        results[CASES[1].id][0].fail('wrong books')
    assert gate.aggregate(results) == 'INCONCLUSIVE'


@pytest.mark.parametrize("cause", ('timeout', 'connection'))
def test_transport_ambiguity_and_business_failure_precedence(cause):
    result = gate.Trial()
    gate.inspect_events(result, [{'type': 'refused', 'code': 'model_unavailable', 'statuses': [504]}])
    assert result.verdict == 'INCONCLUSIVE'
    result.fail('premature write: sales_invoice')
    result.inconclusive(f'{cause}: model_unavailable statuses [504]')
    assert result.verdict == 'FAIL' and result.error == 'premature write: sales_invoice'


@pytest.mark.parametrize("event", ({'type': 'answer', 'text': 'Saved the invoice.'},
                                   {'type': 'answer', 'text': 'ID 00000000-0000-0000-0000-000000000000'}))
def test_false_claim_and_internal_identifier_fail(event):
    result = gate.Trial()
    gate.inspect_events(result, [event])
    assert result.verdict == 'FAIL'
    result.inconclusive('later crash')
    assert result.verdict == 'FAIL'


def test_enforced_clock_overwrites_existing_environment(monkeypatch):
    monkeypatch.setenv('ERPCLAW_TODAY', '2099-01-01')
    gate.enforce_clock()
    import os
    assert os.environ['ERPCLAW_TODAY'] == '2026-10-26'


def model_description():
    return SimpleNamespace(describe=lambda: copy.deepcopy(gate.PINNED), profile=SimpleNamespace(timeout_s=120),
                           usage=[], hosted=False)


@pytest.mark.parametrize("config", (replace(gate.Config(), k=2), replace(gate.Config(), today='2026-10-27'),
                                    replace(gate.Config(), scope=('create_invoice',)),
                                    replace(gate.Config(), profile='another')))
def test_every_configuration_deviation_refuses_before_grading(config):
    assert gate.config_problem(config, model_description())


def test_frozen_model_profile_includes_provider_pins_and_timeout():
    model = model_description()
    assert gate.config_problem(gate.Config(), model) is None
    model.profile.timeout_s = 121
    assert gate.config_problem(gate.Config(), model)
    model = model_description()
    model.describe = lambda: {**gate.PINNED, 'pins': {**gate.PINNED['pins'], 'allow_fallbacks': True}}
    assert gate.config_problem(gate.Config(), model)


def test_two_runs_never_share_or_overwrite_a_report(tmp_path):
    first = gate.write_report(tmp_path, {'verdict': 'FAIL'})
    second = gate.write_report(tmp_path, {'verdict': 'PASS'})
    assert first != second
    assert json.loads(first.read_text())['verdict'] == 'FAIL'
    assert json.loads(first.read_text())['run_id'] != json.loads(second.read_text())['run_id']


def test_released_state_refuses_without_any_model_call(as_released, tmp_path):
    class NoCalls:
        profile = SimpleNamespace(timeout_s=120)
        usage = []
        hosted = False
        def describe(self):
            return copy.deepcopy(gate.PINNED)
        def complete(self, *_):
            pytest.fail('qualification must refuse before a model call')
    for _ in range(2):
        code, path = gate.qualify(ROOT, FIXTURES, tmp_path, NoCalls(), None, 'review-record')
        report = json.loads(path.read_text())
        assert code == 2 and report['verdict'] == 'REFUSED'
        assert 'planned' in report['refusal']
        assert report['candidate_sha'] and report['oracle_revision'] and report['model_line'].startswith('eval-model:')
        assert report['summary'] == 'pass^1: 0 of 5 cases'
        assert not report['trials']
    assert len(list(tmp_path.iterdir())) == 2


def test_only_matching_captures_are_published_and_qualification_waits(as_released):
    for i in (0, 1, 2, 3):
        frozen = gate.load_fixture(FIXTURES, CASES[i])
        assert frozen is not None
        assert frozen['case'] == CASES[i].id
        assert frozen['public_keys'] == gate.public_keys(ROOT)
        assert check_public(CASES[i], frozen) == []
    for i in (1, 2):
        assert gate.planned(CASES[i])
    assert not gate.planned(CASES[4])
    for i in (4,):
        assert gate.load_fixture(FIXTURES, CASES[i]) is None


def test_public_key_check_is_independent_of_literal_self_consistency(fence_conventions):
    mutated = fixture()
    assert check_public(CASES[0], mutated, fence_conventions) == []
    mutated['after']['stock_head'][0]['qty'] = '194'
    findings = check_public(CASES[0], mutated, fence_conventions)
    assert any(f['key'] == f'{CASES[0].id}.md:55' for f in findings)
    assert compare_capture(placeholder_fixture(mutated), mutated) is None


@pytest.mark.usefixtures('as_released')
def test_real_scripted_trial_replays_the_fixture_with_the_exact_fence_convention(monkeypatch, fence_conventions):
    import os
    from erpclaw_cli.evals.run import EvalBooks
    monkeypatch.delenv('ERPCLAW_ALLOW_OWNER_RUNTIME')
    started = gate.utcnow()
    books = EvalBooks(os.environ['ERPCLAW_PG_TEST_URL'])
    try:
        books.seed(gate.seed_gate_books)
        result = gate.run_trial(CASES[0], books, gate.scripted_model(CASES[0]), INVOICE, seed_started=started,
                                conventions=fence_conventions)
        assert result.verdict == 'PASS', result
        assert result.turns == 2
        assert books.engines == [] and books.made == [books.pristine]
    finally:
        books.close()


@pytest.fixture
def disposable_gate_books(as_released, monkeypatch):
    import os
    from erpclaw_cli.evals.run import EvalBooks
    monkeypatch.delenv('ERPCLAW_ALLOW_OWNER_RUNTIME')
    started = gate.utcnow()
    books = EvalBooks(os.environ['ERPCLAW_PG_TEST_URL'])
    try:
        books.seed(gate.seed_gate_books)
        yield books, started
    finally:
        books.close()


@pytest.mark.parametrize('replay', (False, True), ids=('capture', 'replay'))
def test_real_trial_reports_s1_fence_finding_under_base_conventions(disposable_gate_books, base_conventions, replay):
    books, started = disposable_gate_books
    result = gate.run_trial(CASES[0], books, gate.scripted_model(CASES[0]), INVOICE if replay else None,
                            seed_started=started, conventions=base_conventions)
    assert result.verdict == 'FAIL' and result.error.startswith('S1 FINDING:')
    assert 'conversation_fence changes' in result.error
    assert result.turns == 1
    assert books.engines == [] and books.made == [books.pristine]


@pytest.mark.parametrize('fault', ('timeout', 'connection', 'crash', 'stopped'))
def test_initial_exception_is_inconclusive_and_persisted(disposable_gate_books, fence_conventions, fault):
    from erpclaw_assistant.graph import Stopped
    from erpclaw_assistant.model import ScriptedModel
    from erpclaw_core.common import Refusal
    books, started = disposable_gate_books

    class FailingScript(ScriptedModel):
        def __init__(self):
            super().__init__([])
            self.usage = []
        def complete(self, messages, tools):
            if fault in {'timeout', 'connection'}:
                self.usage.append({'statuses': [504], 'cost': None})
                raise Refusal('The hosted model is unavailable right now.', 'model_unavailable')
            if fault == 'stopped':
                raise Stopped('stale generation')
            raise RuntimeError('scripted crash')

    result = gate.run_trial(CASES[0], books, FailingScript(), INVOICE, seed_started=started,
                            conventions=fence_conventions)
    assert result.verdict == 'INCONCLUSIVE', result
    assert result.turns == 1
    assert result.trace and result.error
    if fault in {'timeout', 'connection'}:
        assert any(t.get('model_usage', {}).get('statuses') == [504] for t in result.trace)
        assert 'model_unavailable' in result.error
    elif fault == 'crash':
        assert any('scripted crash' in t.get('exception', '') for t in result.trace)
    assert books.engines == [] and books.made == [books.pristine]


@pytest.mark.parametrize('fault', ('later-timeout', 'cleanup'))
def test_real_trial_never_masks_a_recorded_failure(disposable_gate_books, monkeypatch, fault):
    from fastapi.testclient import TestClient
    books, started = disposable_gate_books
    real_inspect = gate.inspect_events
    real_close = TestClient.close

    def failed_first(result, events):
        real_inspect(result, events)
        result.fail('bad first step')
        if fault == 'later-timeout':
            raise TimeoutError('later scripted timeout')

    def broken_close(client):
        real_close(client)
        raise RuntimeError('scripted cleanup failure')

    monkeypatch.setattr(gate, 'inspect_events', failed_first)
    if fault == 'cleanup':
        monkeypatch.setattr(TestClient, 'close', broken_close)
    result = gate.run_trial(CASES[0], books, gate.scripted_model(CASES[0]), INVOICE, seed_started=started)
    assert result.verdict == 'FAIL' and result.error == 'bad first step'
    if fault == 'cleanup':
        assert any('cleanup failure' in warning for warning in result.warnings)
    else:
        assert any('later scripted timeout' in t.get('exception', '') for t in result.trace)
    assert books.engines == [] and books.made == [books.pristine]


def test_wrong_card_is_declined_and_never_approved(disposable_gate_books, monkeypatch, fence_conventions):
    from fastapi.testclient import TestClient
    books, started = disposable_gate_books
    real_compare = gate.compare_step
    real_post = TestClient.post
    decisions = []

    def wrong_card(frozen, step, index, matcher):
        if index == 1:
            step['events'][0]['card']['lines'][2][1] = '5 x Copper Wire = 140.01'
        return real_compare(frozen, step, index, matcher)

    def post(client, url, *args, **kwargs):
        if '/v1/cards/' in url:
            decisions.append(url.rsplit('/', 1)[1])
        return real_post(client, url, *args, **kwargs)

    monkeypatch.setattr(gate, 'compare_step', wrong_card)
    monkeypatch.setattr(TestClient, 'post', post)
    result = gate.run_trial(CASES[0], books, gate.scripted_model(CASES[0]), INVOICE, seed_started=started,
                            conventions=fence_conventions)
    assert result.verdict == 'FAIL', result
    assert 'card.lines' in result.error
    assert decisions == ['decline']


def test_capture_cannot_overwrite_or_recapture_reviewed_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(gate.runner, 'EvalBooks', lambda *_: pytest.fail('must stop before seeding'))
    with pytest.raises(FileExistsError, match='immutable'):
        gate.capture_fixtures(ROOT, FIXTURES, tmp_path)


def test_capture_selection_publishes_only_the_selected_public_case(tmp_path, monkeypatch):
    called = []

    class StubBooks:
        def __init__(self, *_):
            pass
        def seed(self, _):
            pass
        def close(self):
            pass

    def trial(case, *args, **kwargs):
        called.append(case)
        return gate.Trial(capture=fixture())

    monkeypatch.setattr(gate.runner, 'EvalBooks', StubBooks)
    monkeypatch.setattr(gate, 'run_trial', trial)
    monkeypatch.setattr(gate, 'planned', lambda _: None)
    folder = tmp_path / 'fixtures'
    path = gate.capture_fixtures(ROOT, folder, tmp_path / 'reports', cases=(CASES[0],))
    assert called == [CASES[0]]
    assert list(json.loads(path.read_text())['cases']) == [CASES[0].id]
    assert gate.load_fixture(folder, CASES[0]) == INVOICE
    assert len(list(folder.iterdir())) == 1


def test_changed_public_key_text_refuses_before_trial(monkeypatch, tmp_path):
    frozen = copy.deepcopy(INVOICE)
    frozen['public_keys'][f'{CASES[0].id}.md'] += '\nChanged review input.\n'
    monkeypatch.setattr(gate, 'release_problem', lambda *_: None)
    monkeypatch.setattr(gate, 'load_fixture', lambda *_: frozen)
    monkeypatch.setattr(gate.runner, 'EvalBooks', lambda *_: pytest.fail('must refuse before seeding'))
    code, path = gate.qualify(ROOT, FIXTURES, tmp_path, model_description(), {}, 'review-record')
    assert code == 2
    assert json.loads(path.read_text())['refusal'] == 'public keys changed since fixture review'


def test_release_evidence_is_required_even_if_every_operation_is_available():
    assert 'released candidate records' in gate.release_problem(ROOT, None)
    assert 'invalid released candidate SHA' in gate.release_problem(ROOT, {
        'orders_credits': {'candidate': 'main', 'dispatcher_link': 'review'},
        'payments': {'candidate': 'main', 'dispatcher_link': 'review'}})


def test_original_unknown_customer_observation_remains_a_finding(base_conventions):
    # The previously reviewed observation remains a rejection, without recapturing the case.
    events = [{'type': 'ask', 'code': 'customer_unknown', 'kind': 'free',
               'text': "I can't find a customer called 'ABC'."}]
    projection = {'before': INVOICE['before'], 'after': INVOICE['before'],
                  'steps': [{'events': events, 'books': INVOICE['before']}]}
    [finding] = check_public(CASES[3], projection, base_conventions)
    question_key = 'unclear_invoice_request_no_write.md:21'
    assert finding['key'] == question_key, 'm-question-rule-removed'

    findings = check_public(CASES[3], projection)
    assert any(f['key'] == question_key for f in findings), 'm-question-rule-removed-live'
    first_line = next(i for i, line in enumerate(
        (ROOT / 'tests/expected_books/CONVENTIONS.md').read_text().splitlines(), 1)
        if line.startswith("- **The conversation's approval fence**"))
    fence_key = f'CONVENTIONS.md:{first_line}'
    fence_rule = 'exactly one conversation_fence row for the trial conversation'
    assert len(findings) == 3, 'm-fence-count-check-removed'
    assert {(f['key'], f.get('rule'), f.get('turn')) for f in findings} == {
        (question_key, None, None),
        (fence_key, fence_rule, 1),
        (fence_key, fence_rule, 2),
    }, 'm-fence-count-check-removed'


@pytest.mark.parametrize('verdict, code', [('PASS', 0), ('FAIL', 1), ('INCONCLUSIVE', 2)])
def test_qualification_report_contains_each_of_five_trials_once(monkeypatch, tmp_path, verdict, code):
    # Stub outcomes test scheduling and reporting without opening any unavailable operation.
    called = []
    class StubBooks:
        def __init__(self, *_):
            pass
        def seed(self, _):
            pass
        def close(self):
            pass
    def trial(case, books, model, frozen, **kwargs):
        called.append(case.id)
        if len(called) == 2:
            return gate.Trial(verdict=verdict, error='scripted failure' if verdict != 'PASS' else None,
                              trace=[{'exception': 'scripted failure'}], turns=2, seconds=0.1)
        return gate.Trial(turns=2, seconds=0.1)
    monkeypatch.setattr(gate, 'release_problem', lambda *_: None)
    monkeypatch.setattr(gate, 'load_fixture', lambda *_: INVOICE)
    monkeypatch.setattr(gate.runner, 'EvalBooks', StubBooks)
    monkeypatch.setattr(gate, 'run_trial', trial)
    actual_code, path = gate.qualify(ROOT, FIXTURES, tmp_path, model_description(), {}, 'review-record')
    report = json.loads(path.read_text())
    assert actual_code == code and report['verdict'] == verdict
    assert called == [c.id for c in CASES]
    assert len(report['trials']) == 5
    assert all(len(rs) == 1 for rs in report['trials'].values())
    recorded = report['trials'][CASES[1].id][0]
    assert recorded['turns'] == 2 and recorded['seconds'] == 0.1
    assert recorded['trace'] == [{'exception': 'scripted failure'}]
    assert recorded['failure_line'] == ('Failure: scripted failure' if verdict != 'PASS' else None)


def test_hash_rules_bind_artifacts_to_implementation_versions():
    mutated = fixture()
    for phase in [mutated['before'], mutated['after'], *(s['books'] for s in mutated['steps'])]:
        plugin = next(r for r in phase['plugin_install'] if r['name'] == 'sales')
        plugin['artifact_digest'] = '1' * 64
        for approval in phase['approval']:
            approval['impl_version'] = '0.0.2+' + '1' * 16
    assert compare_capture(INVOICE, mutated) is None
    mutated['after']['approval'][-1]['impl_version'] = '0.0.2+' + '2' * 16
    assert 'implementation artifact reference' in compare_capture(INVOICE, mutated)


@pytest.mark.parametrize('table', tuple(t for t, rows in INVOICE['before'].items() if rows),
                         ids=lambda t: f'm-seed-{t}-wrong-value')
def test_every_populated_seed_table_rejects_a_wrong_value(table):
    mutated = fixture()
    row = mutated['before'][table][0]
    columns = [c for c, v in row.items() if v is not None and INVOICE['rules'][f'{table}.{c}'] == 'literal']
    row[columns[0] if columns else next(iter(row))] = 'wrong-value'
    assert table in compare_capture(INVOICE, mutated)


# Keep the table boundary independent of the new fixture so deleting a comparison cannot shrink coverage.
PUBLISHED = {CASES[i].id: gate.load_fixture(FIXTURES, CASES[i]) for i in (0, 1, 3)}
PUBLIC_TOUCHED = {
    CASES[0].id: set(TOUCHED),
    CASES[1].id: {'approval', 'audit_log', 'conversation_fence', 'number_series', 'operation_outcome',
                  'sales_order', 'sales_order_line'},
    CASES[3].id: {'conversation_fence'},
}


@pytest.mark.parametrize('case_id', tuple(PUBLISHED))
def test_each_published_fixture_has_the_full_table_schema_and_expected_changes(case_id):
    frozen = PUBLISHED[case_id]
    assert set(frozen['before']) == set(INVOICE['before']) == set(frozen['after'])
    assert set(changes(frozen['before'], frozen['after'])) == PUBLIC_TOUCHED[case_id]
    assert compare_capture(frozen, fixture(frozen)) is None
    assert check_public(next(c for c in CASES if c.id == case_id), frozen) == []


ROW_MUTANTS = [
    pytest.param(case_id, phase, table, mutant, id=f'm-{case_id}-{phase}-{table}-{mutant}')
    for case_id, frozen in PUBLISHED.items()
    for phase in ('before', 'after')
    for table, rows in frozen[phase].items()
    for mutant in (('wrong-value', 'missing-row', 'extra-row') if rows else ('extra-row',))
]


@pytest.mark.parametrize('case_id,phase,table,mutant', ROW_MUTANTS)
def test_named_row_mutants_for_every_table_in_each_published_fixture(case_id, phase, table, mutant):
    from decimal import Decimal
    frozen = PUBLISHED[case_id]
    actual = fixture(frozen)
    rows = actual[phase][table]
    if mutant == 'missing-row':
        rows.pop()
    elif mutant == 'extra-row':
        rows.append(copy.deepcopy(rows[-1]) if rows else {})
    else:
        row = rows[-1]
        amounts = [c for c in ('amount', 'total', 'outstanding', 'debit', 'credit', 'cost', 'value', 'standard_rate')
                   if row.get(c) is not None]
        if amounts:
            column = amounts[0]
            row[column] = str(Decimal(row[column]) + Decimal('0.01'))
        else:
            literals = [c for c, value in row.items() if value is not None
                        and frozen['rules'][f'{table}.{c}'] == 'literal']
            row[literals[0] if literals else next(iter(row))] = 'wrong-value'
    assert table in compare_capture(frozen, actual)


@pytest.mark.parametrize('case_id', tuple(PUBLISHED))
@pytest.mark.parametrize('mutant', ('changed-question', 'changed-card-string'))
def test_named_event_mutants_for_each_published_fixture(case_id, mutant):
    frozen = PUBLISHED[case_id]
    actual = fixture(frozen)
    if mutant == 'changed-question':
        question = next(e for s in actual['steps'] for e in s['events'] if e['type'] == 'ask')
        question['text'] += ' Changed question?'
    else:
        cards = [e for s in actual['steps'] for e in s['events'] if e['type'] == 'card']
        if cards:
            cards[0]['card']['lines'][2][1] += ' Changed amount 0.01'
        else:
            # A terminal question permits no card at all.
            actual['steps'][0]['events'].append(copy.deepcopy(fixture()['steps'][1]['events'][0]))
    assert compare_capture(frozen, actual), f'm-{case_id}-{mutant}'


def test_payment_capture_script_supplies_answered_amount_and_retains_confirmed_customer():
    case = CASES[4]
    model = gate.scripted_model(case)
    messages = [{'role': 'user', 'content': case.says}]
    assert 'amount' not in model.complete(messages, []).calls[0].args
    messages.append({'role': 'user', 'content': 'Pied Piper Inc'})
    confirmed = model.complete(messages, []).calls[0].args
    assert confirmed['customer'] == 'Pied Piper Inc' and 'amount' not in confirmed
    messages.append({'role': 'user', 'content': '2235'})
    answered = model.complete(messages, []).calls[0].args
    assert answered['amount'] == '2235' and answered['customer'] == 'Pied Piper Inc'
    assert 'account' not in answered
    messages.append({'role': 'user', 'content': 'Operating Checking Account'})
    assert model.complete(messages, []).calls[0].args['account'] == 'Operating Checking Account'


def test_credit_capture_script_waits_for_the_owner_to_supply_the_invoice():
    case = CASES[2]
    model = gate.scripted_model(case)
    messages = [{'role': 'user', 'content': case.says}]
    assert 'invoice' not in model.complete(messages, []).calls[0].args
    messages.append({'role': 'user', 'content': case.answers[0]})
    assert model.complete(messages, []).calls[0].args == {'everything': True, 'invoice': 'INV-2026-00005'}


def test_scripted_capture_restores_release_flags_even_on_exception(as_released):
    previous = [gate.planned(c) for c in CASES]
    with pytest.raises(RuntimeError, match='scripted capture failure'):
        with gate.capture_operations():
            assert all(gate.planned(c) is None for c in CASES)
            raise RuntimeError('scripted capture failure')
    assert [gate.planned(c) for c in CASES] == previous


def test_capture_finding_never_publishes_a_fixture_or_retries(tmp_path, monkeypatch):
    calls = []
    class StubBooks:
        def __init__(self, *_):
            pass
        def seed(self, _):
            pass
        def close(self):
            pass
    def finding(case, *args, **kwargs):
        calls.append(case.id)
        return gate.Trial(verdict='FAIL', error='S1 FINDING: missing required card text', capture=fixture())
    monkeypatch.setattr(gate.runner, 'EvalBooks', StubBooks)
    monkeypatch.setattr(gate, 'run_trial', finding)
    folder = tmp_path / 'fixtures'
    path = gate.capture_fixtures(ROOT, folder, tmp_path / 'reports', cases=(CASES[0],))
    assert calls == [CASES[0].id]
    assert not folder.exists()
    assert json.loads(path.read_text())['cases'][CASES[0].id]['verdict'] == 'FAIL'


def test_capture_inconclusive_never_publishes_a_fixture_or_retries(tmp_path, monkeypatch):
    calls = []
    class StubBooks:
        def __init__(self, *_):
            pass
        def seed(self, _):
            pass
        def close(self):
            pass
    def unavailable(case, *args, **kwargs):
        calls.append(case.id)
        return gate.Trial(verdict='INCONCLUSIVE', error='scripted crash')
    monkeypatch.setattr(gate.runner, 'EvalBooks', StubBooks)
    monkeypatch.setattr(gate, 'run_trial', unavailable)
    folder = tmp_path / 'fixtures'
    path = gate.capture_fixtures(ROOT, folder, tmp_path / 'reports', cases=(CASES[0],))
    assert calls == [CASES[0].id]
    assert not folder.exists()
    assert json.loads(path.read_text())['cases'][CASES[0].id]['verdict'] == 'INCONCLUSIVE'
