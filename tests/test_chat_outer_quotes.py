"""Outer punctuation is removed without changing quoted words inside a request."""
import pytest
from erpclaw_assistant.graph import _reading_text


@pytest.mark.parametrize('said, expected', [
    ('Add a customer called "Lakeside Cafe", ap@lakeside.example"',
     'Add a customer called "Lakeside Cafe", ap@lakeside.example'),
    ("Add a customer called O'Neil, ap@oneil.example'", "Add a customer called O'Neil, ap@oneil.example"),
    ('Add a customer called "Lakeside Cafe"', 'Add a customer called "Lakeside Cafe"'),
    ("Add a customer called 'Lakeside Cafe'", "Add a customer called 'Lakeside Cafe'"),
    ('“Add a customer called Lakeside Cafe”', 'Add a customer called Lakeside Cafe'),
    ('‘Add a customer called Lakeside Cafe’', 'Add a customer called Lakeside Cafe'),
])
def test_outer_marks_keep_internal_quotes(said, expected):
    assert _reading_text(said).strip() == expected


def test_mismatched_boundary_quotes_are_not_a_pair():
    said = '"Add a customer called Lakeside Cafe\''
    assert _reading_text(said) == said
