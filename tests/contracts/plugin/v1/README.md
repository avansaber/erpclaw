# Plugin contract fixtures, version plugin/1

The plugin interface every plugin builds against, frozen as `PLUGIN_CONTRACT = "plugin/1"` in
`erpclaw_core.plugins`: the manifest schema (with `services` and `uses`), the loader's refusals, `Contract`'s fields
and hooks, plugin migration branches, the service declaration and registration, `call_service`, and checks registered
with `Ctx.register_check`. Each JSON file here is one case with its expected outcome; `tests/test_plugin_contract_v1.py`
runs every file against the engine. A change to the engine that fails a fixture changes the interface.

## Format

| Field | Meaning |
|---|---|
| `contract` | always `plugin/1` |
| `part` | `manifest`, `loader`, `branch`, `contract`, `service`, `check` or `question` |
| `name` | the file's name, unique |
| `expect` | the outcome the engine must give |
| `note` | what the case pins down |

By part:

- **`manifest`**: a `manifest` as JSON. `expect` is its `digest` (and, with `differs_from` or
  `differs_from_manifest`, a digest it must differ from), or `invalid`: the field the schema refuses. The files named
  `*_before_services` hold manifests and digests written before services existed: a manifest with no services and no
  uses keeps that digest, so books installed then still open.
- **`loader`**: `plugins`, each described by `name`, optional `contracts` (`name`, `effect`, the `hooks` it has,
  optional `plugin`), `metrics`, `slots`, `services` (`name`, `effect`, `input` and `output` model kinds: `closed`,
  `open`, `nested_open`, `not_a_model`; `fn` false for none), `tables` (`name`, `owner`, `existing` for a table already
  on the metadata), `migrations` (`extra_branch`), and `manifest`, the fields that override the manifest derived from
  those objects. They are loaded with the engine's real plugins. With `entry_point` (`dist`, and `object`: `plugin`,
  `other` or `raises`), the one plugin comes through an entry point instead. `expect` is `loads` or `refused`: text
  the refusal contains.
- **`branch`**: a plugin owning `owned_tables`, with `revisions` (the source of each revision file of its branch, or
  null for none). `expect` is `loads` or `refused`.
- **`contract`**: `contract_interface` lists the names this version freezes. The others run a contract of the planted
  consumer plugin (`tests/service_plugins.py`), named by `operation`, in demo books: `args`, optional `planned`, and the card, read set,
  status and text, or the run's text and data, or the `refusal` code.
- **`service`**: `caller` calls `service` with `input` on a context that is `plain`, a `preview`, or an approved card's
  `commit`; optional `uses` (the caller's uses instead of every provider service) and `disable` (a plugin whose install
  is disabled first). `expect` is the `output`, exactly, or the `refusal`: its `code` and, where given, its `message`,
  `options` and `data` (complete, refs included). Only plain JSON-shaped data crosses, both ways, checked by
  exact type before any validation: a dict with plain text keys, a list, text, an integer, a bool, None and a
  Decimal. Everything else is refused: a float, a tuple, a set, bytes, an iterator, a model instance (a service
  returns plain data and dumps its own model with `model_dump(mode="json", by_alias=True)`), and every subclass (an
  enum member, an OrderedDict, a str or dict subclass). The plain data is validated by the model, by alias; the
  caller receives the model's `model_dump(mode="json", by_alias=True)`, which runs the model's own serializers and
  is checked by exact type again. Names in inputs and outputs are a field's alias where it has one, else its name. In an output, `@acct:<number>` stands for that account's id in the books.
- **`check`**: the consumer's write contract (`operation`) is proposed and approved with `args`; its write service registers a
  check under each key of `checks`, in order, and a check whose key starts with `refuse` refuses. `expect` is the
  outcome's `status` and `code`, the keys of the checks that `ran`, in order, and whether the posting stayed.
- **`question`**: the question a service raises reaches the owner when `operation` is asked for: `where` is `effect` (the write service raises it
  inside the approved commit, after the caller's writes) or `proposal` (the provider's read service is made to raise
  it while the card is proposed). `expect.event` is the API's event, field by field: each choice of the question by its `code` and `label` only, never its `ref`, which stays in the stored outcome and the conversation note.

## The planted plugins

`svc_provider` exposes `quote` (read: an amount times a rate), `record` (write: two ledger lines for the caller's one
posting; registers the checks it is given; raises the question when asked) and `probe` (read: returns a good or a
wrong output on request). `svc_consumer` depends on it, lists all three in `uses`, and has a write contract
`svc_post` and a read contract `svc_read` that call them. Every amount crosses a service as `Money`, `{"value": "2.00",
"currency": "USD"}`, nested ones included (`ledger_lines[].debit` and `credit`): a bare amount, a float, a value
beyond the currency's scale and a currency the books do not have (`unknown_currency`) are refused.

## Changing a fixture

Version `plugin/1` is frozen. A change that breaks a plugin is a new version, served beside this one until every
plugin has moved, each plugin's owner acknowledging a tested commit; this folder is then kept until the old version
is removed.
