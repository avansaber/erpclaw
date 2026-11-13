# The service's control pipe

How ERPClaw's window (the desktop host) talks to the background service (`erpclaw-service`). The code is
`src/ErpClaw.Service.Core/Control.cs` (`ControlProtocol`, `ControlServer`) and `Supervisor.cs` (what each request does).
The two must stay in step with this page.

## Transport

- A Windows named pipe that the service serves. It is created **once**, with `FILE_FLAG_FIRST_PIPE_INSTANCE`,
  `PIPE_REJECT_REMOTE_CLIENTS` and a protected DACL that grants only the owner's SID. It has one instance, and its
  connections are served one after another (connect, serve, disconnect). The service never creates a second instance.
- **Peer check before anything is read.** Per connection, the service calls `GetNamedPipeClientProcessId`. The
  client's user SID must equal the service's, and its image must be the signed host executable in the program folder
  (on the Store route: the package family and the installed location). If the client fails the check, the service
  reads no byte, writes nothing and closes the connection.
- The pipe's name is `\\.\pipe\erpclaw-service-<user SID>-<32 lower-case hex characters>` (128 random bits). It is new at
  every service start, and the host finds it in `run\service.json` (see "The service file"). The name is never logged.

## Framing

**One connection carries one request and one response.**

1. The host writes **one JSON object on one line**: UTF-8, ending in `\n` (0x0A), with no `\n` inside. The line can
   be at most **4096 bytes**, the newline included.
2. The service reads up to the newline and nothing past it. It runs the request and writes **one JSON object on one
   line**, ending in `\n`.
3. The service closes the connection.

If the line is longer than the limit, has no newline before EOF, is not valid UTF-8, or is not exactly one JSON
object, the answer is `bad_request`. The service accepts no fields beyond the ones listed for each op.

## Requests

Every request has `"op"`. A request may also carry `"challenge"`: base64 of 16 to 64 random bytes. When it does, the
response carries `"proof"`: base64 of HMAC-SHA256(key, `"erpclaw-service-check-v1"` followed by the challenge bytes).
The host compares the proof in constant time before it uses anything in the response.

The key is the 32 bytes that `service_secret` writes as 64 lower-case hex characters (the bytes, not the text). The
label is the 24 ASCII characters `erpclaw-service-check-v1`. The peer check always comes first: a client that fails it
is closed before any byte, a challenge included, is read.

| op | Other fields | What the service does | `result` on success |
|---|---|---|---|
| `status` | none | Answers at once | none; the response has `state` and `reason` |
| `mint_code` | `principal`, `run_id` (both required) | Refuses unless `run_id` is the engine's current run (the run the host has just verified). Otherwise mints with core's mint function (the engine's `service mint-code`) | none; the response has `code` |
| `list_backups` | none | `erpclaw service list-backups` | the command's stdout object |
| `backup_now` | none | `erpclaw service backup --kind manual`, with the engine running | the command's stdout object |
| `restore` | `backup` (a file name from `list_backups`) | `restore-stage` with the engine running, then stops the engine, `switch --to <staged>`, starts the engine. If the switch fails, the engine starts again on the old books | `switch`'s stdout object |
| `remove_replaced` | `name` | `erpclaw service remove-replaced --name`. An exit 4 with `"partial"` is run again, 3 runs in all, 1 s and then 2 s apart | the command's stdout object |
| `quit` | none | Answers, then stops cleanly: the engine first, then PostgreSQL | none |

Argument values (`principal`, `backup`, `name`) are 1 to 256 characters, never start with `-` and contain no control
characters. `run_id` is 1 to 128 characters from `A-Z a-z 0-9 - _`.

`status`, `mint_code` and `list_backups` are answered at once. `backup_now`, `restore` and `remove_replaced` wait in
line with the service's own work (a restart, the daily backup), so a `restore` can take minutes. The host should not
time them out early.

## Responses

Success:

    {"ok":true,"result":{...}}
    {"ok":true,"code":"ABCDEFGHIJKLMNOPQRSTUVWXYZ"}          (mint_code)
    {"ok":true,"state":"running"}                           (status; "reason" when it gave up)

`state` is one of `stopped`, `starting`, `running`, `restarting`, `restoring`, `stopping`, `gave_up`.

Failure: `{"ok":false,"error":"<code>","text":"<a plain sentence for the owner>","reason":"<the command's reason code, when it gave one>"}`

| error | When |
|---|---|
| `bad_request` | The request is malformed: see Framing and the field rules above |
| `unknown_op` | The service does not know the op |
| `unavailable` | The service is starting, restarting, restoring, stopping or has given up (`text` is then the plain reason) |
| `stale_run` | `mint_code`'s `run_id` is not the engine's current run. The host verifies the engine again and asks again |
| `unknown_principal` | `mint_code` for a principal the books do not have. No code is minted |
| `refused` | An engine command exited 2. `reason` and `text` are the command's |
| `could_not_run` | An engine command exited 3 (disk, database down, a backup that gave up on its capture deadline); try again later. Also used for a mint that failed |
| `failed_unchanged` | An engine command exited 4 and changed nothing, or remove_replaced was still partial after its runs |
| `internal` | The command exited 1 or printed something other than one JSON object, or the service hit an internal error. `text` is "ERPClaw hit an internal error"; details go only to the log |
| `not_restarted` | The restore switched the books, but the engine did not start again. `text` is the plain reason |

A sign-in code appears only in the `mint_code` response line. It is never logged, never put on a command line, and
never shown in an exception or a `ToString`.

## Example

    > {"op":"mint_code","principal":"owner","run_id":"0f1e2d3c4b5a69788796a5b4c3d2e1f0","challenge":"q83vEjRWeJCrze8SNFZ4kA=="}
    < {"ok":true,"code":"MZXW6YTBOI234567ABCDEFGHIJ","proof":"..."}

## The service file

`run\service.json` tells the host where the pipe is and gives it the key to check the service with. It holds exactly
three keys, all strings, and is read under the same rules as the engine's run file: one JSON object, at most 4096
bytes, each key once, nothing else in it.

| key | value |
|---|---|
| `control_pipe` | `\\.\pipe\erpclaw-service-<user SID>-<32 lower-case hex>`, new at every service start |
| `service_secret` | 64 lower-case hex characters (32 random bytes), new at every service start |
| `database_port` | PostgreSQL's port on 127.0.0.1, as decimal text (1 to 65535, no leading zero) |

- The file is created with an owner-only, protected access list in the creating call itself (never made first and
  restricted after), under a temporary name, and renamed into place. A new pipe name and a new secret are made at every
  service start, and the file is written again (same pipe, same secret) only when the port changes.
- The service deletes any file left by a crash when it starts, and deletes its file when it stops.
- The service's process ids and state are in another file, `service-state.json`. That file holds no secret.
- The service never logs the pipe's name or the secret. The log also refuses a line that holds a port, a sign-in code,
  a database address, a `key=value` connection parameter, a `PG...` environment variable name or a pipe path.
- What this does not protect: a process running as the owner's own account can read the file, as it can read the
  engine's run file and the credential store. With the file it can pose as the host to the service (mint a code, ask for
  a backup or a restore, quit). Another Windows account cannot read the file or open the pipe.

## Sign-in codes

`mint_code` runs the engine's `service mint-code`, which prints `{"code": ...}` on its stdout. The service reads that
output into memory, passes the code to the host in the response and drops it. It never writes the code to its log, a
file or a command line. The service logs only `mint-code ok`, or `mint-code exit <n>` with the command's exit code.

## Stopping the engine

The engine waits on a named event, `Local\erpclaw-stop-<run id>`, that it creates with an owner-only access list. To stop
it the service sets that event and waits up to 15 seconds for the engine to exit; it kills the engine only if it has not
exited by then, if its run id is not known yet, or if the event cannot be opened.
