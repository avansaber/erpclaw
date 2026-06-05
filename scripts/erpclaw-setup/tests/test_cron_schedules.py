"""M8 phase C — cron schedule registration (Part A).

The two email background workers (`process-email-queue`, `process-drip-sends`)
have no auto-installed scheduler: ERPClaw's only active scheduling path is an
explicit `openclaw cron add` command, documented in the foundation
(erpclaw-setup) SKILL.md "Optional scheduling" section. SKILL.md `cron:` blocks
are decorative and never auto-register (CHANGELOG v4.1.0 / F1 remediation), so
the SKILL.md command is the registration surface.

These tests parse that SKILL.md and assert both cron entries are registered with
the correct action target and interval (1 min / 5 min). They never invoke the
real workers — job execution is out of scope here (mocked by simply not running
it), so this stays deterministic and offline in CI.
"""
import os
import re

_SETUP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# .../erpclaw/scripts/erpclaw-setup  ->  .../erpclaw/SKILL.md (foundation == erpclaw-setup)
_FOUNDATION_SKILL_MD = os.path.join(
    os.path.dirname(os.path.dirname(_SETUP_DIR)), "SKILL.md"
)

# Expected schedules per the WAVE_0 M8-C DoD.
#   process-email-queue every 1 minute  -> "* * * * *"
#   process-drip-sends   every 5 minutes -> "*/5 * * * *"
_EXPECTED = {
    "process-email-queue": ("* * * * *", 1),
    "process-drip-sends": ("*/5 * * * *", 5),
}


def _parse_cron_entries():
    """Return {action: cron_expr} for every `openclaw cron add` documented in the
    foundation SKILL.md. Backslash line-continuations are joined first so a
    single logical command spanning two physical lines parses as one."""
    with open(_FOUNDATION_SKILL_MD, encoding="utf-8") as fh:
        text = fh.read()
    # Join shell line-continuations ("\<newline>") into one logical line.
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    entries = {}
    pattern = re.compile(
        r"openclaw cron add\b[^\n]*?"
        r'--cron\s+"(?P<cron>[^"]+)"[^\n]*?'
        r'--message\s+"Using erpclaw, run the (?P<action>[a-z-]+) action\."'
    )
    for m in pattern.finditer(joined):
        entries[m.group("action")] = m.group("cron")
    return entries


def _interval_minutes(cron_expr):
    """Minimal cron-minute-field reader: supports `*` (every minute) and `*/N`."""
    minute_field = cron_expr.split()[0]
    if minute_field == "*":
        return 1
    m = re.fullmatch(r"\*/(\d+)", minute_field)
    if m:
        return int(m.group(1))
    raise AssertionError(f"unsupported minute field: {minute_field!r}")


def test_foundation_skill_md_exists():
    assert os.path.exists(_FOUNDATION_SKILL_MD), _FOUNDATION_SKILL_MD


def test_both_cron_entries_registered():
    entries = _parse_cron_entries()
    for action in _EXPECTED:
        assert action in entries, (
            f"{action} cron not registered in foundation SKILL.md "
            f"(found: {sorted(entries)})"
        )


def test_email_queue_runs_every_minute():
    entries = _parse_cron_entries()
    expected_expr, expected_min = _EXPECTED["process-email-queue"]
    assert entries["process-email-queue"] == expected_expr
    assert _interval_minutes(entries["process-email-queue"]) == expected_min


def test_drip_sends_runs_every_five_minutes():
    entries = _parse_cron_entries()
    expected_expr, expected_min = _EXPECTED["process-drip-sends"]
    assert entries["process-drip-sends"] == expected_expr
    assert _interval_minutes(entries["process-drip-sends"]) == expected_min


def test_no_decorative_cron_yaml_block():
    """Guard the F1 remediation (CHANGELOG v4.1.0): scheduling must stay prose
    `openclaw cron add`, never a structured `cron:` SKILL.md block that the
    ClawHub analyzer reads as a scheduled financial mutation."""
    with open(_FOUNDATION_SKILL_MD, encoding="utf-8") as fh:
        lines = fh.readlines()
    offenders = [ln for ln in lines if re.match(r"\s*cron:\s*$", ln)]
    assert not offenders, f"decorative cron: block reintroduced: {offenders}"
