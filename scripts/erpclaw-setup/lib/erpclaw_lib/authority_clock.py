"""Host wall-clock for authorization timestamps, in milliseconds."""

import time

EXACT_APPROVAL_DEFAULT_MS = 600_000
EXACT_APPROVAL_MAX_MS = 3_600_000
ROUTINE_MAX_MS = 86_400_000


def now_ms():
    """Current host UTC wall-clock time in milliseconds."""
    return time.time_ns() // 1_000_000
