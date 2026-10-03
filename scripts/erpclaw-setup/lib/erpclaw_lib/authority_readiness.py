"""Readiness signal for the ACTIVE phase."""


def is_ready(conn):
    """Whether an ACTIVE install is ready to serve.

    No qualified readiness evaluator exists yet; until one does, ACTIVE
    is never ready. A test that patches this to True is "not
    qualification" and proves nothing about readiness.
    """
    return False
