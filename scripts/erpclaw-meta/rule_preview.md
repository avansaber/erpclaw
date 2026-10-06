# Business-rule preview

`evaluate-rule --rule-json '<JSON>' --facts-json '<JSON>'` compiles explicit
JSON conditions and evaluates supplied facts without reading records or writing
to the database. Use it to check a proposed alert, workflow condition or score
predicate before configuring the owning application.

Example: a proposed invoice exceeds the reviewed limit and is still a draft.

```json
{"conditions":[{"field":"total","operator":">","value":"500.00"},{"field":"status","operator":"=","value":"draft"}],"match":"all"}
```

Facts: `{"total":"500.01","status":"draft"}`. The result is `matched: true`
with a separate result for every condition. `match` accepts `all` (the default)
or `any`; empty rules refuse. Every predicate is validated, including one after
an already matched `any` predicate. A missing field does not match, including
an inequality, and is labelled `missing`.

The operators are `=`, `!=`, `>`, `>=`, `<`, `<=`, `contains` and `in`.
Ordered comparisons use exact Decimal strings or integers, up to 30 whole and
12 fractional digits. JSON fractional numbers, non-finite values and exponents
refuse. Equality and membership compare both type and value, so `"1.00"` differs
from `"1"` and `true` differs from `1`. Use ordered numeric predicates when
decimal formatting should not affect the comparison. `contains` is a
case-insensitive text comparison. `in` takes a nonempty scalar list.

Facts are flat scalar fields, never executable expressions or attribute paths.
The preview accepts at most 100 conditions, 100 facts and 32768 characters per
JSON argument. Duplicate keys and unknown condition fields refuse. Its
`preview_only` result does not enforce a policy, certify the supplied facts,
change a stored rule, award a score or dispatch an action. Existing persisted
workflow, alert and lead-score bindings remain separate and are not replaced.
