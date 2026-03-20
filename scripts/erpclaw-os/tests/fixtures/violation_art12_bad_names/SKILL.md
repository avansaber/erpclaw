---
name: violart12claw
version: 1.0.0
description: Violation fixture for Article 12 testing
author: test
scripts:
  - scripts/db_query.py
---

# violart12claw

Fixture module with bad action names (camelCase, missing prefix).

## Actions

| Action | Description |
|--------|-------------|
| `add-widget` | Missing namespace prefix |
| `update_widget` | Uses underscore not kebab |
| `status` | Status |
