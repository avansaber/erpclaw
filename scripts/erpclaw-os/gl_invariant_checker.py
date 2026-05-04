"""Thin facade — gl_invariants moved to erpclaw_lib for cross-skill access.

Foundation's `scripts/erpclaw-os/gl_invariant_checker.py` and addon's
`sandbox.py` both import from `erpclaw_lib.gl_invariants`. This file
preserves backward compatibility for any caller still using the old
import path.
"""
from erpclaw_lib.gl_invariants import (
    check_gl_invariants,
)

__all__ = ["check_gl_invariants"]
