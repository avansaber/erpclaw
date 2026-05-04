"""ERPClaw shared library — used by all ERPClaw skills.

Version is the canonical lib version, tracked alongside foundation SKILL.md
version. The bootstrap self-heal compares this constant against the deployed
marker at ~/.openclaw/erpclaw/lib/.erpclaw_lib_version on every foundation
action invocation.
"""

__version__ = "4.1.0"
