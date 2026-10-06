# Audit checkpoints

`get-audit-checkpoint` returns a SHA-256 of the complete current audit table,
including every stored column and row. It does not expose the audit content,
change records or create an audit entry for its own read. Keep the digest and
algorithm in a trusted record outside this database.

Later, run `get-audit-checkpoint --audit-checkpoint-sha256 <retained digest>`.
`checkpoint_matches` reports whether the full table is unchanged. A mismatch
can be an authorised append, a correction, an altered record or a deletion.
Investigate it before replacing the trusted checkpoint. A new digest without a
trusted earlier one cannot establish whether old records were altered.

The digest orders rows by their text ids and covers their exact stored TEXT
values, nulls and column names. Changing JSON spacing, actor metadata or a
timestamp changes the digest. Row insertion order does not. Schema upgrades
that change the stored columns also change the checkpoint. The command refuses
above 100000 rows rather than returning a partial digest. An empty table is
explicitly labelled `empty`.

This is an operator-retained checkpoint, not a signed record, automatic chain,
retention policy, access-control grant or certification of the audit's origin.
Someone able to replace both the database and its trusted checkpoint can hide
a change. Continue the existing backup and access-control procedures.
