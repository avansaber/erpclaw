"""CLI chat window over the engine API (embedded by default, or --url)."""
from __future__ import annotations

import re
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from erpclaw_cli import journal

# a traceback never shows its locals: a command may hold a URL or password in one
app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False, help="ERPClaw: an ERP you run by chat")
con = Console()


def _books_url(owner: bool = False) -> str:
    """The books' PostgreSQL URL for this command (the owner's for one that creates tables); a URL
    that is missing or not PostgreSQL stops the command with the plain message."""
    from erpclaw_core.db import RoleRefused, database_url, migration_url

    try:
        return migration_url() if owner else database_url()
    except RoleRefused as e:
        con.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


def _client(url: str | None):
    """The engine API over HTTP with the principal's token (ERPCLAW_TOKEN), or in-process as the owner: the
    OS account that runs the engine is trusted there."""
    if url:
        import os

        import httpx
        token = os.environ.get("ERPCLAW_TOKEN")
        if not token:
            con.print("[red]Set ERPCLAW_TOKEN to your API token (from `erpclaw users add` or `init`).[/red]")
            raise typer.Exit(1)
        return httpx.Client(base_url=url, timeout=600, headers={"Authorization": f"Bearer {token}"})
    from fastapi.testclient import TestClient

    from erpclaw_api.app import create_app
    from erpclaw_assistant.muse import model_from_env
    from erpclaw_core.db import make_engine
    # an exception inside the engine is the server's error (a 500), never a traceback in the window
    return TestClient(create_app(make_engine(_books_url()), model_from_env(), embedded_owner=True),
                      raise_server_exceptions=False)


# What the window prints is plain text: no terminal escapes or control characters (newline and tab kept), and no
# console markup, whatever a reply, a name or a note contains. Kept here so the window imports nothing of the engine.
_ESCAPES = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]|(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?|\x1b[@-_]?")
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _plain(value) -> str:
    return escape(_CONTROLS.sub("", _ESCAPES.sub("", str(value))))


def show_card(card: dict) -> None:
    t = Table.grid(padding=(0, 2))
    for k, v in card["lines"]:
        t.add_row(f"[dim]{_plain(k)}[/dim]", _plain(v))
    from rich.console import Group
    books = "\n".join(_plain(b) for b in card["books"])
    parts = [t, f"\n[bold]Books[/bold]\n{books}"]
    if card["warnings"]:
        parts.append("\n[yellow]" + "\n".join("! " + _plain(w) for w in card["warnings"]) + "[/yellow]")
    if card["footer"]:
        parts.append(f"\n[dim]{_plain(card['footer'])}[/dim]")
    con.print(Panel(Group(*parts), title=f"[bold]{_plain(card['title'])}[/bold]", border_style="green", expand=False))


CARD_PROMPT = "Approve (a), decline (d), or type a change"
APPROVE, DECLINE = ("a", "approve"), ("d", "decline")
NUMBERED = ("field", "approval", "capability")  # a free question numbers nothing: a number is its answer
UNSAFE = "I couldn't record your approval safely, so I didn't send it."
LOST = "I sent your approval but didn't get an answer. Check what happened? (y/n)"
UNREACHABLE = ("I still can't reach the books; your approval may or may not have been saved. I'll check again "
               "next time.")
LATER = "I'll check it again when you next start the chat."
CHECKING = "An approval from {at} wasn't confirmed. Checking it now ..."
ALREADY = "That approval is already being sent from another window, so I didn't send it again."
RECORD_LOST = ("I sent your approval but couldn't keep its record safely. It may or may not have been saved. "
               "Check the card in the books before trying again.")
NO_ANSWER = "The engine couldn't answer that just now, so nothing was changed. Please send it again."
UNCONFIRMED = ("The latest response did not confirm your approval; it may or may not have been saved. "
               "I'll check it again next time.")


def _reply_events(r) -> list[dict]:
    """A reply's events; any other answer (a refusal, a malformed request, a server error) said in plain words."""
    if r.status_code == 200:
        return r.json()["events"]
    try:
        detail = r.json().get("detail")
    except ValueError:
        detail = None
    if r.status_code >= 500:
        detail = None  # a failure before any commit is said in the client's own words: nothing was changed
    con.print(f"[yellow]{_plain(detail) if isinstance(detail, str) and detail else NO_ANSWER}[/yellow]")
    return []


def _ordered(choices: list[dict]) -> list[dict]:
    """A question's choices as numbered: the candidates in order, then the control actions."""
    return [c for c in choices if not c.get("control")] + [c for c in choices if c.get("control")]


def show_ask(e: dict) -> None:
    con.print(f"[cyan]{_plain(e['text'])}[/cyan]")
    choices = e.get("choices") or []
    if e.get("kind") in NUMBERED:
        for i, ch in enumerate(_ordered(choices), 1):
            con.print(f"  {i}. {_plain(ch.get('label', ''))}")
    elif controls := [ch for ch in choices if ch.get("control")]:  # named only, never numbered
        con.print("[dim]  or: " + ", ".join(_plain(ch.get("label", "")) for ch in controls) + "[/dim]")


def message_for(question: dict | None, typed: str) -> dict:
    """What the owner typed, as the message to send: a number alone at a numbered question is that choice's code,
    with its label as the text; anything else, and anything at a free question, is the owner's own words."""
    said = typed.strip()
    if question and question.get("kind") in NUMBERED and said.isdigit():
        ordered = _ordered(question.get("choices") or [])
        if 1 <= int(said) <= len(ordered):
            ch = ordered[int(said) - 1]
            return {"text": ch.get("label") or said, "choice": ch["code"]}
    return {"text": typed}


def _ask(text: str) -> str | None:
    try:
        return Prompt.ask(escape(text), console=con)
    except (EOFError, KeyboardInterrupt):
        return None


def _card_answer() -> str | None:
    """At a card: a or approve, d or decline, or the owner's change; an empty answer asks again; None at the end
    of input, which leaves the card as it is."""
    while True:
        said = _ask(CARD_PROMPT)
        if said is None or said.strip():
            return said


def _send_approval(c, aid: str, digest: str, cid: str | None):
    """The response if one arrived; None for a transport failure or timeout."""
    import httpx

    try:
        r = c.post(f"/v1/cards/{aid}/approve", params={"conversation_id": cid}, json={"digest": digest})
    except httpx.TransportError:
        return None
    return r


_TERMINAL_APPROVAL_RESULTS = frozenset(("committed", "refused", "superseded", "expired", "declined", "invalidated"))


def _approval_outcome_events(response):
    """A parseable approval outcome, or None when this reply proves nothing."""
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("events"), list) or not body["events"]:
        return None
    events = body["events"]
    authoritative = False
    changed = False
    fresh_card = False
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return None
        kind = event["type"]
        if kind == "result":
            if not isinstance(event.get("status"), str) or event["status"] not in _TERMINAL_APPROVAL_RESULTS \
                    or not isinstance(event.get("text"), str):
                return None
            if event["status"] == "committed" and not isinstance(event.get("operation_key"), str):
                return None
            authoritative = True
        elif kind in ("ask", "refused"):
            if not isinstance(event.get("text"), str) or not isinstance(event.get("code"), str):
                return None
            if event.get("status") in ("superseded", "refused"):
                authoritative = True
            elif kind == "refused" and event["code"] == "card_changed":
                changed = True
            else:
                return None
            if kind == "ask":
                choices = event.get("choices") or []
                if not isinstance(choices, list) or any(not isinstance(ch, dict)
                        or not isinstance(ch.get("label"), str)
                        or ((event.get("kind") in NUMBERED or "code" in ch)
                            and not isinstance(ch.get("code"), str))
                        for ch in choices):
                    return None
        elif kind == "card":
            card = event.get("card")
            if not isinstance(event.get("approval_id"), str) or not isinstance(event.get("digest"), str) \
                    or not isinstance(card, dict):
                return None
            if not isinstance(card.get("title"), str) or not isinstance(card.get("footer"), str) \
                    or not isinstance(card.get("lines"), list) or not isinstance(card.get("books"), list) \
                    or not isinstance(card.get("warnings"), list):
                return None
            if any(not isinstance(line, (list, tuple)) or len(line) != 2 for line in card["lines"]):
                return None
            fresh_card = True
        elif kind == "email" and not isinstance(event.get("text"), str):
            return None
    return events if authoritative or (changed and fresh_card) else None


def _answered(c, j, entry, r) -> dict | None:
    """Finish only after a parseable authoritative outcome was shown."""
    events = _approval_outcome_events(r)
    if events is None:
        try:
            body = r.json()
        except ValueError:
            body = None
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, str) and detail.strip():
            con.print(f"[yellow]Latest attempt response: {_plain(detail)}[/yellow]")
        _keep_unconfirmed(j, entry, f"[yellow]{UNCONFIRMED}[/yellow]")
        return None
    try:
        question = show_events(c, entry.data["conversation_id"], events, j)
    except Exception:
        # An output or continuation failure must not delete the receipt or
        # contradict an outcome that may already have been shown.
        j.keep(entry)
        raise
    j.finish(entry)
    return question


def _keep_unconfirmed(j, entry, message: str) -> None:
    """A future recovery promise needs the record still owned when the attempt ends."""
    owned = j.owns(entry)
    j.keep(entry)
    con.print(message if owned else f"[red]{RECORD_LOST}[/red]")


def approve(c, j, cid: str, e: dict) -> dict | None:
    """Approve a card through the journal (published, locked, then sent); a lost reply is checked by sending the
    same approval again on the owner's y. The last question shown, if any."""
    try:
        entry = j.publish(e["approval_id"], e["digest"], cid)
    except journal.AlreadySending:
        con.print(f"[yellow]{ALREADY}[/yellow]")
        return None
    except journal.NotRecorded:
        con.print(f"[red]{UNSAFE}[/red]")
        return None
    if not j.owns(entry):  # no first attempt: the owner can still be told it was not sent
        j.keep(entry)
        con.print(f"[red]{UNSAFE}[/red]")
        return None
    r = _send_approval(c, e["approval_id"], e["digest"], cid)
    if (r is None or r.status_code >= 500) and not j.owns(entry):  # the attempt may have run
        j.keep(entry)
        con.print(f"[red]{RECORD_LOST}[/red]")
        return None
    if r is None or r.status_code >= 500:
        if (_ask(LOST) or "").strip().lower() not in ("y", "yes"):
            _keep_unconfirmed(j, entry, LATER)
            return None
        if not j.owns(entry):
            j.keep(entry)
            con.print(f"[red]{RECORD_LOST}[/red]")
            return None
        r = _send_approval(c, e["approval_id"], e["digest"], cid)
        if r is None:
            _keep_unconfirmed(j, entry, f"[yellow]{UNREACHABLE}[/yellow]")
            return None
    return _answered(c, j, entry, r)


def recover(c, j) -> tuple[str | None, dict | None]:
    """At the chat's start: each unconfirmed approval of these books checked by sending it again; the engine says
    what happened to that card. The conversation to continue in, when a check ended on a question, and it."""
    from datetime import datetime

    cid, question = None, None
    for entry in j.recover():
        at = datetime.fromisoformat(entry.data["time"]).astimezone().strftime("%H:%M")
        con.print(f"[dim]{CHECKING.format(at=at)}[/dim]")
        if not j.owns(entry):  # gone or replaced since it was locked: not this window's to send
            j.keep(entry)
            continue
        r = _send_approval(c, entry.data["approval_id"], entry.data["digest"], entry.data["conversation_id"])
        if r is None:
            _keep_unconfirmed(j, entry, f"[yellow]{UNREACHABLE}[/yellow]")
            continue
        asked = _answered(c, j, entry, r)
        if asked:
            cid, question = entry.data["conversation_id"], asked
    return cid, question


def show_events(c, cid: str, events: list[dict], j=None) -> dict | None:
    """Print a turn's events; at a card, the owner approves, declines or types a change (sent as the next message).
    The question the events end on, if they end on one, for a number typed next."""
    question = None
    for e in events:
        kind = e.get("type")
        if kind not in {
            "card", "refused", "ask", "email", "result", "answer", "notice", "not_done"
        }:
            continue
        question = None
        if kind == "card":
            show_card(e["card"])
            said = _card_answer()  # your answer, here; never text in the conversation
            if said is None:
                return None
            if said.strip().lower() in APPROVE:
                question = approve(c, j, cid, e)
            elif said.strip().lower() in DECLINE:
                r = c.post(f"/v1/cards/{e['approval_id']}/decline", params={"conversation_id": cid})
                question = show_events(c, cid, _reply_events(r), j)
            else:  # a change: the owner's next message, whose card (if any) sets this one aside
                with con.status("thinking"):
                    r = c.post(f"/v1/conversations/{cid}/messages", json={"text": said})
                question = show_events(c, cid, _reply_events(r), j)
        elif kind in ("refused",):
            con.print(f"[yellow]{_plain(e['text'])}[/yellow]")
        elif kind == "ask":
            show_ask(e)
            question = e
        elif kind == "email":
            con.print(f"[green]{_plain(e['text'])}[/green]")
        elif kind == "result":
            style = "green" if e["status"] == "committed" else "yellow"
            con.print(f"[{style}]{_plain(e['text'])}[/{style}]")
        else:  # an answer, notice, or not_done event
            con.print(_plain(e.get("text", "")))
    return question


DATE_QUESTION = "How do you write dates: month first (10/5 is 5 October) or day first (10/5 is 10 May)? (m/d/skip)"
DATE_ANSWERS = {"m": "MDY", "d": "DMY", "skip": None}
DATE_ORDER_HELP = "How you write dates: MDY (month first) or DMY (day first)"


def _date_order(value: str) -> str:
    """The --date-order flag's value, MDY or DMY in any case; anything else stops the command, nothing changed."""
    order = value.strip().upper()
    if order not in ("MDY", "DMY"):
        con.print(f"[red]--date-order is MDY (month first) or DMY (day first), not {_plain(value)}. "
                  "Nothing was changed.[/red]")
        raise typer.Exit(2)
    return order


def _ask_date_order() -> str | None:
    """The owner's answer to the date question; no answer at all (end of input) leaves it unset, like skip."""
    while True:
        try:
            said = Prompt.ask(escape(DATE_QUESTION), console=con)
        except EOFError:
            return None
        answer = said.strip().lower()
        if answer in DATE_ANSWERS:
            return DATE_ANSWERS[answer]
        con.print("Please answer m (month first), d (day first) or skip.")


@app.command()
def init(date_order: str = typer.Option(None, "--date-order", help=f"{DATE_ORDER_HELP}; asked when omitted"),
         own_books: bool = typer.Option(False, "--own-books", help="Your own books instead of the demo: one company "
                                        "with no business choices yet, set up in chat on one card")):
    """Create books in an empty PostgreSQL database: demo books (Stark Manufacturing), or your own with --own-books."""
    from erpclaw_core.db import holds_tables, make_engine, runtime_role, safe_url
    from erpclaw_core.grants import apply_grants
    from erpclaw_sales.demo import seed_demo
    if own_books and date_order is not None:
        con.print("[red]Your own books get their date order in chat, on the setup card; leave out --date-order. "
                  "Nothing was changed.[/red]")
        raise typer.Exit(2)
    order = _date_order(date_order) if date_order is not None else None
    url = _books_url(owner=True)  # as the owner: init creates tables
    role = runtime_role()
    shown = safe_url(url)
    eng = make_engine(url)  # refuses books this engine does not own
    if own_books:
        _init_own_books(eng, role, shown)
        return
    if holds_tables(eng):
        eng.dispose()
        con.print(f"[yellow]{shown} already holds books. init fills an empty database only; "
                  "nothing was changed.[/yellow]")
        raise typer.Exit(1)
    if date_order is None:
        order = _ask_date_order()  # never taken from the currency: the owner says, or it stays unset
    seed_demo(eng)
    from erpclaw_core.administration import issue_token, set_date_order
    from erpclaw_core.common import company_ctx

    with eng.begin() as conn:
        if order is not None:
            set_date_order(conn, company_ctx(conn).company_id, order)
        token = issue_token(conn, "owner")
        apply_grants(conn, role)
    con.print(f"[green]Demo books ready:[/green] {shown}")
    if order is None:
        con.print("No date order is set, so a date such as 10/5 will be asked. Set it with "
                  "`erpclaw company set --date-order MDY` (month first) or `DMY` (day first).")
    else:
        con.print(f"Dates are read {'month' if order == 'MDY' else 'day'} first ({order}).")
    con.print(f"The owner's API token, shown once (for `chat --url`, as ERPCLAW_TOKEN): [bold]{token}[/bold]")


def _init_own_books(eng, role: str, shown: str) -> None:
    """The trusted bootstrap of the owner's own books: no business choice is made here, and nothing is printed that the
    owner must keep (the launcher signs the owner in). Books that already hold a company are refused; a schema with no
    company (a bootstrap that stopped) is finished."""
    from erpclaw_core.common import Refusal
    from erpclaw_core.setup import create_own_books

    try:
        create_own_books(eng, runtime_role=role)  # the books and the runtime role's grants, in one transaction
    except Refusal as r:
        eng.dispose()
        con.print(f"[yellow]{_plain(r.message)} Nothing was changed.[/yellow]")
        raise typer.Exit(1) from None
    con.print(f"[green]Your books are ready to set up:[/green] {shown}")
    con.print("Start `erpclaw chat`: a few questions about your business come first, then one card to approve.")


@app.command()
def grants():
    """Apply and verify the PostgreSQL runtime role's privileges (as the owner)."""
    from erpclaw_core.db import make_engine, runtime_role
    from erpclaw_core.grants import apply_grants
    from erpclaw_core.plugins import check_installed, load_plugins

    url = _books_url(owner=True)
    role = runtime_role()
    load_plugins()
    eng = make_engine(url)
    check_installed(eng)
    with eng.begin() as conn:
        apply_grants(conn, role)
    con.print(f"[green]The runtime role {role} holds exactly its policy.[/green]")


def _refuse_owner_credential() -> None:
    """The long-running commands never carry the owner credential."""
    import os

    if os.environ.get("ERPCLAW_MIGRATION_URL"):
        con.print("[red]ERPCLAW_MIGRATION_URL is set: the owner credential must not reach the engine's "
                  "long-running process. Unset it and run with ERPCLAW_DATABASE_URL only.[/red]")
        raise typer.Exit(1)


def _draw_port(generator) -> int:
    """Draw from the dynamic range, leaving its final 64 ports unused."""
    return generator.randint(49152, 65471)


@app.command()
def serve(host: str = "127.0.0.1", port: int | None = None, run_file: Path | None = None, tcp: bool = False):
    """Run the engine API."""
    _refuse_owner_credential()
    if host != "127.0.0.1":
        typer.echo("ERPClaw serves this machine only in this version; a remote engine is a later design.")
        raise typer.Exit(2)
    # Retain refusal of an explicitly configured foreign books URL.
    import os

    if os.environ.get("ERPCLAW_DATABASE_URL"):
        _books_url()
    if (port is None and run_file is None) or (port is not None and run_file is not None):
        typer.echo("Choose --port or --run-file with --tcp to launch the engine.")
        raise typer.Exit(2)
    if run_file is not None and not tcp:
        typer.echo("This build has no pipe listener yet; add --tcp for a loopback listener.")
        raise typer.Exit(2)
    if port is not None and not 1 <= port <= 65535:
        typer.echo("The port must be between 1 and 65535.")
        raise typer.Exit(2)
    import errno
    import secrets
    import socket

    import uvicorn

    from erpclaw_api.pipe import served_app
    from erpclaw_api.runfile import check_run_file_path, remove_if_unchanged, write_run_file
    from erpclaw_assistant.muse import model_from_env
    from erpclaw_core.db import make_engine
    from erpclaw_core.outbox import delivery_driver
    if run_file is not None:
        try:
            check_run_file_path(run_file)
        except (OSError, ValueError) as exc:
            typer.echo(str(exc))
            raise typer.Exit(2) from None
    engine, model = make_engine(_books_url()), model_from_env()
    from erpclaw_core.administration import end_all_sessions_at_start

    try:
        end_all_sessions_at_start(engine)
    except Exception:
        typer.echo("The engine could not end earlier sessions.")
        raise typer.Exit(1) from None
    generator = secrets.SystemRandom() if port is None else None
    for _ in range(32 if port is None else 1):
        run_port = _draw_port(generator) if port is None else port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                sock.bind(("127.0.0.1", run_port))
            except OSError as exc:
                if port is None and exc.errno == errno.EADDRINUSE:
                    continue
                break
            if run_file is not None:
                try:
                    written = write_run_file(run_file, {"port": run_port})
                except (OSError, ValueError):
                    typer.echo("The engine could not write the run file.")
                    raise typer.Exit(1) from None
            else:
                typer.echo(f"http://127.0.0.1:{run_port}", err=True)
            try:
                application = served_app(engine, model, port=run_port)
                with delivery_driver(engine):
                    uvicorn.run(application, fd=sock.fileno(), log_level="warning", ws="none")
            finally:
                if run_file is not None:
                    remove_if_unchanged(run_file, written)
            return
    typer.echo("The engine could not open a loopback listener.")
    raise typer.Exit(1)


def _books_key(url: str | None) -> str:
    """Which books a journal entry belongs to: the API URL, or the in-process books' address; either without its
    user, password or query (which can carry credentials)."""
    if url:
        from urllib.parse import urlsplit, urlunsplit

        u = urlsplit(url)
        return urlunsplit((u.scheme, u.netloc.rpartition("@")[2], u.path, "", ""))
    try:
        from erpclaw_core.db import database_url, safe_url

        return f"in-process {safe_url(database_url())}"
    except Exception:  # no books configured here: the in-process books, unnamed
        return "in-process"


@app.command()
def chat(url: str = typer.Option(None, help="Engine API URL; omit to run the engine in this process")):
    """Chat with your books."""
    _refuse_owner_credential()
    chat_loop(_client(url), books=_books_key(url))


def chat_loop(c, books: str | None = None) -> None:
    """The chat window on an API client, until the owner types quit: first each unconfirmed approval is checked."""
    with journal.Journal(journal.folder(), books or _books_key(None)) as j:
        cid, question = recover(c, j)
        cid = cid or c.post("/v1/conversations").json()["id"]
        con.print("[dim]ERPClaw. Type 'quit' to leave.[/dim]")
        while True:
            try:
                text = Prompt.ask("[bold]you[/bold]")
            except (EOFError, KeyboardInterrupt):
                break
            if text.strip().lower() in ("quit", "exit"):
                break
            if not text.strip():
                continue
            with con.status("thinking"):
                r = c.post(f"/v1/conversations/{cid}/messages", json=message_for(question, text))
            question = show_events(c, cid, _reply_events(r), j)


@app.command(name="eval")
def run_eval(k: int = typer.Option(3, help="Trials per case"),
             only: str = typer.Option(None, help="Comma-separated case ids")):
    """Run the eval set on fresh books with the configured model."""
    from erpclaw_cli.evals import run
    code = run.main(k=k, only=only.split(",") if only else None)
    raise typer.Exit(code)


users_app = typer.Typer(help="Who may do what in these books (as the owner): add, grant, revoke, disable")
app.add_typer(users_app, name="users")


def _perms(can: str) -> list[str]:
    return [x.strip() for x in can.split(",") if x.strip()]


def _admin(fn):
    """Run an owner-only change in one transaction, refusals as plain messages."""
    from erpclaw_core.common import Refusal

    eng = _books(owner=True)
    try:
        with eng.begin() as conn:
            return fn(conn)
    except Refusal as r:
        con.print(f"[red]{r.message}[/red]")
        raise typer.Exit(1)


def _company_id(conn, company: str | None) -> str:
    from erpclaw_core.common import company_ctx

    return company_ctx(conn, company_name=company).company_id


@users_app.command("add")
def users_add(name: str, can: str = typer.Option(..., help="Comma-separated: view,draft,approve,post,..."),
              company: str = typer.Option(None, help="The company, if the books hold more than one")):
    """Add a principal and print its API token once (only a hash is stored)."""
    from erpclaw_core.administration import add_principal

    token = _admin(lambda conn: add_principal(conn, name, _perms(can), [_company_id(conn, company)]))
    con.print(f"[green]Added {name}.[/green] API token, shown once: [bold]{token}[/bold]")


@users_app.command("grant")
def users_grant(name: str, can: str = typer.Option(...), company: str = typer.Option(None)):
    """Grant permissions; the principal's pending cards can no longer be approved."""
    from erpclaw_core.administration import grant

    _admin(lambda conn: grant(conn, name, _perms(can), _company_id(conn, company)))
    con.print(f"[green]Granted {can} to {name}.[/green]")


@users_app.command("revoke")
def users_revoke(name: str, can: str = typer.Option(...), company: str = typer.Option(None)):
    """Revoke permissions; the principal's pending cards can no longer be approved."""
    from erpclaw_core.administration import revoke

    _admin(lambda conn: revoke(conn, name, _perms(can), _company_id(conn, company)))
    con.print(f"[green]Revoked {can} from {name}.[/green]")


@users_app.command("disable")
def users_disable(name: str):
    """Disable a principal: its token stops working and its pending cards can no longer be approved."""
    from erpclaw_core.administration import disable

    _admin(lambda conn: disable(conn, name))
    con.print(f"[green]Disabled {name}.[/green]")


@users_app.command("sign-in")
def users_sign_in(name: str, cancel: bool = typer.Option(False, "--cancel", help="Cancel unused sign-in codes")):
    """Mint a sign-in code shown once, or cancel this principal's unused codes."""
    from erpclaw_core.administration import cancel_sign_in_codes, mint_sign_in_code
    from erpclaw_core.principals import OWNER

    if cancel:
        count = _admin(lambda conn: cancel_sign_in_codes(conn, name, OWNER))
        con.print(f"Cancelled {count} sign-in codes for {_plain(name)}.")
    else:
        code = _admin(lambda conn: mint_sign_in_code(conn, name, OWNER))
        con.print(f"Sign-in code: {code}\nIt lasts 10 minutes and works once.", markup=False)


@users_app.command("sign-out")
def users_sign_out(name: str):
    """End every open session of this principal."""
    from erpclaw_core.administration import sign_out_principal
    from erpclaw_core.principals import OWNER

    count = _admin(lambda conn: sign_out_principal(conn, name, OWNER))
    con.print(f"Ended {count} sessions for {_plain(name)}.")


@users_app.command("sessions")
def users_sessions(name: str = typer.Argument(None)):
    """List open sessions, optionally for one principal."""
    from erpclaw_core.administration import list_sessions

    rows = _admin(lambda conn: list_sessions(conn, name))
    table = Table("Principal", "Created", "Last seen", "User agent")
    for row in rows:
        table.add_row(_plain(row["principal"]), row["created_at"].isoformat(), row["last_seen_at"].isoformat(),
                      _plain(row["user_agent"] or ""))
    con.print(table)


company_app = typer.Typer(help="The company's settings (as the owner)")
app.add_typer(company_app, name="company")


@company_app.command("set")
def company_set(date_order: str = typer.Option(..., "--date-order", help=DATE_ORDER_HELP),
                company: str = typer.Option(None, help="The company, if the books hold more than one")):
    """Change how dates in requests are read (audited); stored dates never change."""
    from erpclaw_core.administration import set_date_order

    order = _date_order(date_order)
    old = _admin(lambda conn: set_date_order(conn, _company_id(conn, company), order))
    if old == order:
        con.print(f"Dates were already read {'month' if order == 'MDY' else 'day'} first ({order}); nothing changed.")
    else:
        con.print(f"[green]Dates are now read {'month' if order == 'MDY' else 'day'} first ({order}).[/green]")


plugins_app = typer.Typer(help="Plugins in these books: list, install, disable, uninstall")
app.add_typer(plugins_app, name="plugins")


def _books(owner: bool = False):
    """The books, opened only when their plugin installs match this engine: as
    the runtime role, or as the owner for a command that creates tables."""
    from erpclaw_core.db import RoleRefused, make_engine, migration_url
    from erpclaw_core.files import check_root
    from erpclaw_core.grants import validate_runtime_session
    from erpclaw_core.plugins import BooksRefused, check_installed, load_plugins

    load_plugins()
    try:
        eng = make_engine(migration_url() if owner else None)
        if not owner:
            validate_runtime_session(eng)
        check_installed(eng)
        check_root(eng)
    except (BooksRefused, RoleRefused) as e:
        con.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    return eng


files_app = typer.Typer(help="The books' stored files: the owner's retention job")
app.add_typer(files_app, name="files")


@files_app.command("retire")
def files_retire():
    """Remove stored files past their retention date that no message names, and leftovers no row names (as the
    owner)."""
    from erpclaw_core.retention import retire

    out = retire(_books(owner=True))
    con.print(f"Retired {out['retired']} stored file rows past their date; skipped {out['skipped_named_by_messages']} "
              f"that a message names; removed {out['files_removed']} files and {out['leftovers_removed']} leftovers.")
    for name in out["restore_marks"]["removed"]:
        con.print(f"Removed the files a crashed restore left: files/{name}")
    for name in out["restore_marks"]["roots_without_database"]:
        con.print(f"files/{name} has no database on this server; it is kept (it may belong to another server).")


@plugins_app.command("list")
def plugins_list():
    """Plugins this engine has, and their state in these books."""
    from sqlalchemy import select

    from erpclaw_core import models as m
    from erpclaw_core.plugins import REGISTRY

    eng = _books()
    with eng.connect() as conn:
        rows = {r["name"]: r["status"] for r in conn.execute(select(m.plugin_install)).mappings()}
    t = Table("Plugin", "Version", "In these books")
    for name in REGISTRY.order:
        if name != "core":
            t.add_row(name, REGISTRY.plugins[name].version, rows.get(name, "not installed"))
    con.print(t)


@plugins_app.command("install")
def plugins_install(name: str, yes: bool = typer.Option(False, "--yes", help="Install after the preview")):
    """Show what installing adds; install only with --yes."""
    from erpclaw_core.common import Refusal
    from erpclaw_core.db import runtime_role
    from erpclaw_core.plugins import REGISTRY, install_plugin, install_preview

    eng = _books(owner=True)
    p = REGISTRY.plugins.get(name)
    if p is None or name == "core":
        con.print(f"[red]This engine has no plugin called {name}.[/red]")
        raise typer.Exit(1)
    pv = install_preview(p)
    con.print(f"Installing {pv['plugin']} {pv['version']} adds:")
    con.print(f"  tables: {', '.join(pv['tables']) or 'none'}")
    con.print(f"  operations: {', '.join(f'{n} ({e})' for n, e in pv['operations']) or 'none'}")
    con.print(f"  metrics: {', '.join(pv['metrics']) or 'none'}")
    con.print(f"  slots: {', '.join(pv['slots']) or 'none'}")
    if not yes:
        con.print("Nothing was installed. Run again with --yes to install.")
        return
    try:
        install_plugin(eng, name, runtime_role())
    except Refusal as e:
        con.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    con.print(f"[green]{name} installed.[/green]")


def _lifecycle(fn, name: str, done: str) -> None:
    from erpclaw_core.common import Refusal

    eng = _books()
    try:
        with eng.begin() as conn:
            fn(conn, name)
    except Refusal as e:
        con.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    con.print(f"[green]{name} {done}.[/green]")


@plugins_app.command("disable")
def plugins_disable(name: str):
    """Stop a plugin's operations in these books; its data stays readable."""
    from erpclaw_core.plugins import disable_plugin

    _lifecycle(disable_plugin, name, "disabled")


@plugins_app.command("uninstall")
def plugins_uninstall(name: str):
    """Remove a plugin from these books; refused while posted documents depend on it."""
    from erpclaw_core.plugins import uninstall_plugin

    _lifecycle(uninstall_plugin, name, "uninstalled; its tables and data are kept")


@app.command("journal-csv")
def journal_csv_cmd(from_date: str = typer.Option(..., "--from", help="First date, YYYY-MM-DD (inclusive)"),
                    to_date: str = typer.Option(..., "--to", help="Last date, YYYY-MM-DD (inclusive)"),
                    out: Path = typer.Option(..., "--out", help="New CSV file; never overwritten"),
                    company: str = typer.Option(None, "--company", help="Company name"),
                    as_name: str = typer.Option("owner", "--as", help="Principal holding view and export")):
    """Export a company's posted journal to CSV (read-only)."""
    from erpclaw_core.common import Refusal
    from erpclaw_core.journal_export import export_journal

    eng = _books(owner=False)
    try:
        with eng.connect() as conn:
            export_journal(conn, out, from_date=from_date, to_date=to_date,
                           company_name=company, principal=as_name)
    except (Refusal, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    typer.echo("Journal CSV written.")


@app.command("backup")
def backup_cmd(path: Path):
    """Back up these books to a zip file (not encrypted: keep it where the books are kept)."""
    import os
    import tempfile
    import zipfile

    from erpclaw_core.backup import NOTE, BackupRefused, backup

    target = path if path.suffix == ".zip" else path.with_suffix(".zip")
    eng = _books()
    with tempfile.TemporaryDirectory() as tmp:
        try:
            backup(eng, Path(tmp) / "books")
        except BackupRefused as e:
            con.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
        try:  # created private and exclusive: never readable by others, never over an existing file
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            con.print(f"[red]{target} already exists; a backup never overwrites.[/red]")
            raise typer.Exit(1)
        with os.fdopen(fd, "wb") as out, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            root = Path(tmp) / "books"
            for f in sorted(root.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(root).as_posix())
    con.print(f"[green]Backup written:[/green] {target}")
    con.print(NOTE)


def _restore_target(to_database: str, runtime_role: str | None):
    """New books for restore: a new database on the owner's server.
    Never ERPCLAW_DATABASE_URL: restore does not open the books in use."""
    import os

    from erpclaw_core.backup import NewPostgresDatabase
    from erpclaw_core.db import RoleRefused, require_postgresql

    server = os.environ.get("ERPCLAW_MIGRATION_URL")
    if not server:
        con.print("[red]Restoring runs as the owner role: set ERPCLAW_MIGRATION_URL.[/red]")
        raise typer.Exit(2)
    try:
        require_postgresql(server)
    except RoleRefused as e:
        con.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    return NewPostgresDatabase(server, to_database, runtime_role)


def _unpacked(path: Path, tmp: str) -> Path:
    import shutil

    if path.suffix == ".zip":
        shutil.unpack_archive(str(path), tmp, "zip")
        return Path(tmp)
    return path


@app.command("restore")
def restore_cmd(path: Path,
                to_database: str = typer.Option(..., "--to-database", help="Name of a NEW PostgreSQL database"),
                runtime_role: str = typer.Option(None, "--runtime-role", help="PostgreSQL role the engine runs as"),
                upgrade: bool = typer.Option(False, "--upgrade", help="Upgrade the restored books to this release")):
    """Restore a backup into NEW books; never over existing ones. Switching to them is up to you."""
    import tempfile

    from erpclaw_core.backup import BackupRefused, restore

    if upgrade:
        from erpclaw_core.restore_upgrade import restore

    target = _restore_target(to_database, runtime_role)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            result = restore(_unpacked(path, tmp), target)
        except BackupRefused as e:
            con.print(f"[red]Nothing was published: {e}[/red]")
            if "exists" in str(e):
                con.print("If an earlier restore's outcome is uncertain, check it with erpclaw restore-verify.")
            raise typer.Exit(1)
    con.print(f"[green]Restored and proven:[/green] {result.location}")
    con.print("To use these books: stop the engine, point ERPCLAW_DATABASE_URL at them, start it. "
              "The old books are unchanged.")


@app.command("restore-verify")
def restore_verify_cmd(path: Path, database: str = typer.Option(..., "--database")):
    """Whether books equal a backup and pass the integrity checks (read-only)."""
    import tempfile

    from erpclaw_core.backup import BackupRefused, verify

    target = _restore_target(database, None)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            diffs = verify(_unpacked(path, tmp), target)
        except BackupRefused as e:
            con.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
    if diffs:
        con.print("[red]These books do not equal the backup:[/red]")
        for d in diffs:
            con.print(f"  {d}")
        raise typer.Exit(1)
    con.print("[green]The books equal the backup and pass the integrity checks.[/green]")


@app.command("restore-cleanup")
def restore_cleanup_cmd(yes: bool = typer.Option(False, "--yes", help="Remove without asking")):
    """Remove what crashed restores left behind on the owner's server: marked staging databases, and the files
    folders this server's own restores marked and no longer hold."""
    import os

    from erpclaw_core.backup import BackupRefused, clean_restored_files, clean_staging, stale_stagings

    url = os.environ.get("ERPCLAW_MIGRATION_URL")
    if not url:
        con.print("[red]Set ERPCLAW_MIGRATION_URL to clean the server.[/red]")
        raise typer.Exit(2)
    marks = clean_restored_files(url)  # only what a restore of this server marked and no longer holds
    for name in marks["removed"]:
        con.print(f"Removed the files a crashed restore left: files/{name}")
    for name in marks["roots_without_database"]:
        con.print(f"files/{name} has no database on this server; it is kept (it may belong to another server).")
    stale = stale_stagings(url)
    if not stale:
        con.print("Nothing to clean.")
        return
    for item in stale:
        con.print(f"  {item}")
    if not yes and not Confirm.ask("Remove these?", default=False):
        con.print("Nothing was removed.")
        return
    for item in stale:
        try:
            clean_staging(item, url)
        except BackupRefused as e:
            con.print(f"[red]{e}[/red]")
    con.print("[green]Cleaned.[/green]")


from erpclaw_cli.demo import demo_app  # noqa: E402  (its commands use this module's window)

app.add_typer(demo_app, name="demo")


def run() -> None:
    """The command's entry point: a refused plugin or refused books stop it
    with the reason, not a traceback."""
    from erpclaw_core.plugins import BooksRefused, PluginRefused

    from erpclaw_core.db import RoleRefused

    try:
        app()
    except (PluginRefused, BooksRefused, RoleRefused) as e:
        con.print(f"[red]{e}[/red]")
        raise SystemExit(1)


if __name__ == "__main__":
    run()
