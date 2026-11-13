# Updating ERPClaw on existing books (manual runbook)

## Why this exists

ERPClaw checks, every time it opens your books, that the program's plugins are the ones your books were set up with. When a release switches on something new inside a plugin (payments in the sales plugin, goods received in the stock plugin), that plugin's code changes, and the new program refuses to open your existing books. That refusal protects you: it stops a newer program from writing to books it hasn't checked.

Until ERPClaw has its own update command, this runbook is how you move existing books to a new release. You never update books in place. You make a backup with the program you have, restore it with the new program into NEW books, check that the new books match, and then switch. Your old books, old folder and old program stay exactly as they were until you decide to remove them.

The owner does the steps marked **Owner**. An operator can do the rest. Nothing here ever needs a password or key typed into a file, a chat or this document. Database logins come from the settings you already use.

## What you need

- The program you run today (the "old program"), in its own folder.
- The new release, installed in a SEPARATE folder. Never install it over the old one.
- Your database owner login, available as `ERPCLAW_MIGRATION_URL` in the shell you run the steps from (the same setting `erpclaw init` uses). The engine's day-to-day login stays as it is.
- Free disk space for one backup and one more copy of your books.
- About 30 minutes during which nobody uses ERPClaw.

Below, OLD_HOME is your current books folder (your `ERPCLAW_HOME`), and NEW_HOME is a new folder for the updated books. Both must be folders of their own: NEW_HOME and the backup file must NOT be inside OLD_HOME, and OLD_HOME must not be inside NEW_HOME.

Set these absolute paths to your actual folders before starting:

    export OLD_HOME="$ERPCLAW_HOME"
    export NEW_HOME=/absolute/path/to/new/books

Verify that `OLD_HOME` is absolute too. Keep your usual owner login settings available securely; after any chat step, set ERPCLAW_MIGRATION_URL again from those settings before owner maintenance.

## Step 1. Stop everything (Owner)

Close every ERPClaw chat window, stop `erpclaw serve` if you run it, and stop any scheduled job that uses ERPClaw. Then check that nothing is still running:

    ps -eo pid,args | grep -i '[e]rpclaw'
    lsof +D "$OLD_HOME"

Both must print nothing. If either prints a process, stop it and check again. Don't go on while anything is running: the steps below copy files that a running program could be changing.

## Step 2. Nothing may be waiting for your approval (Owner)

An update must not start while a card is waiting for you, or while the approvals journal holds anything at all.

1. In the database, as the owner, read-only. Configure libpq's `PGHOST`, `PGPORT`, `PGUSER`, and `PGDATABASE` for the old database, and `PGPASSFILE` for a protected password file (mode 0600). Use your existing secure credentials; do not put a password or URL in the command. `psql` does not accept the SQLAlchemy driver spelling in ERPCLAW_MIGRATION_URL:

       psql -w -c "SELECT count(*) FROM approval WHERE status = 'pending'"

   It must print 0.
2. The approvals journal folder must be EMPTY:

       ls -A "$OLD_HOME/approvals"

   It must print nothing. ANY file there stops the update: an entry for another set of books, an entry the program couldn't read, or one that is locked. Don't delete or rename journal files to get past this step.

If either check fails, first run `unset ERPCLAW_MIGRATION_URL`, then open chat with the OLD program, approve or decline each waiting card, close chat, and repeat Steps 1 and 2. If the journal still isn't empty and you don't know why, stop and ask for help; don't continue.

## Step 3. Back up with the OLD program (Owner)

Use the program you have today, with your usual settings:

    erpclaw backup /path/outside/old/home/books-before-update.zip

Then make sure the file and its folder are written to disk, and check the file:

    sync -f /path/outside/old/home/books-before-update.zip
    sync -f /path/outside/old/home
    python3 -m zipfile -t /path/outside/old/home/books-before-update.zip
    sha256sum /path/outside/old/home/books-before-update.zip

`zipfile -t` must report no errors. Write down the checksum.

**Owner attests the source:** write down, in your own words, which books this backup came from: the database name in your `ERPCLAW_DATABASE_URL` and the date and time. The engine can check that a backup belongs to these books' lineage, but it can't tell two copies of the same books apart (a restored copy keeps the same identity). Only you can say which database you backed up.

As an extra safety copy you can also keep a `pg_dump` of the old database, stored with the same care as the backup.

## Step 4. Make the new books folder (Operator)

With everything still stopped (Step 1), copy the old books folder to the new one plainly. Don't open the assistant's store with any program while copying.

    mkdir "$NEW_HOME"
    cp -aL "$OLD_HOME"/. "$NEW_HOME"/
    diff -r "$OLD_HOME" "$NEW_HOME"

The copy resolves symbolic links and copies their targets, so the new home does not share those files with the old home. If copying encounters a broken link, a cycle, or any other error, stop. `diff -r` must print nothing. If the old folder holds files beside the database (a SQLite store's `-wal` or `-shm` files, or a `-journal` file), they're copied along with it, unchanged. If you see a `-journal` file and aren't sure why, stop and ask: it can mean a write was interrupted.

## Step 5. Restore into NEW books with the new program (Operator)

Select the new home FIRST, because restore writes the books' stored files under whichever `ERPCLAW_HOME` is set:

    export ERPCLAW_HOME="$NEW_HOME"

Then, from the NEW program's folder (its Python environment), restore the backup into a NEW database. Pick a name that doesn't exist yet, for example `erpclaw_YYYYMMDD`. The runtime role is the one your engine already uses.

    erpclaw restore /path/outside/old/home/books-before-update.zip --to-database erpclaw_YYYYMMDD --runtime-role your_runtime_role --upgrade

This restores the backup into new books at the versions it was made with, proves them equal to the backup, upgrades them to the new release, checks them, and only then publishes them under the new name. If it stops with a message, nothing is switched and your old books are untouched. If it was interrupted and you aren't sure whether it finished, run `erpclaw restore-cleanup` (owner login) to remove half-made leftovers, then repeat this step.

## Step 6. Check the new books against the old ones (Operator, read-only)

With the NEW program's environment selected, export `OLD_DATABASE_URL` and `NEW_DATABASE_URL` from your existing secure settings. Do not put either URL in a command argument. Identify the precise schema changes in the migration files between the old and new revisions, including plugin migrations. Add an `--expect-added-table schema.table` for each new table and `--expect-added-column schema.table.column` for each new column. Only add names established by those migrations; if you cannot establish them, stop and ask for help. For example, a core upgrade from 0017 to 0018 includes these columns (add any plugin migration additions too):

    python tools/compare_books.py --expect-differ public.plugin_install --expect-differ public.alembic_version \
      --expect-added-column public.supplier_payable.posting_id \
      --expect-added-column public.supplier_payable.control_line_no \
      --expect-added-column public.supplier_payable.source_voucher_id \
      --expect-added-column public.supplier_payable.opening_sequence \
      --expect-added-column public.supplier_payable.old_reference \
      --expect-added-column public.supplier_payable.due_date

The command compares every existing book table's row count and shared columns, and reports tables or columns missing on either side, including a column added by hand that the backup left out. It must exit successfully; `plugin_install` and `alembic_version` may differ because the update rewrites them. Added-table allowances apply only to tables on the new side; added-column allowances do not exempt existing rows or columns. For tables without a primary key, row order depends on the server's collation.

The comparison checks the restore’s authentication changes: open sessions are ended with reason `restored` and unused sign-in codes are marked used. All other fields and rows in those tables must match. If the comparison reports a difference outside the exact expected migration changes, stop and ask for help before switching.

Then open the new books with the new program once: with NEW_HOME, the new database and the new program selected, run `unset ERPCLAW_MIGRATION_URL`, start chat, ask a read-only question such as "show my customers", and quit. It must open without a refusal and answer correctly. Don't make any change in this check.

If the comparison fails, don't switch. Your old books are still in use and untouched.

## Step 7. Switch (Owner)

Change your settings by hand, all three together:

1. The database name in `ERPCLAW_DATABASE_URL` (and anywhere else your setup names it): the new database.
2. `ERPCLAW_HOME`: NEW_HOME.
3. Your launcher or shortcut: the new program's folder.

Run `unset ERPCLAW_MIGRATION_URL`, start chat and ask a question you know the answer to (for example your customer list). If the answer is right, you're done.

## Going back

Point all three settings back to the old database, OLD_HOME and the old program's folder. Nothing in the old books was changed, so they work exactly as before. Anything you entered into the new books after the switch is not in the old ones.

## Keep the old books

Keep the old database, OLD_HOME, the old program and the backup file until you decide to remove them. Nothing in this runbook deletes them, and nothing should without your say.

## What this does and doesn't prove

- It proves that the new books equal the BACKUP, then that they were upgraded and checked by the new program.
- It does NOT prove that the backup held everything in the old database. The old program backs up the tables and columns it knows about. The comparison reports a column added by hand that the backup omitted, provided it is not incorrectly listed as an expected migration addition.
- It does NOT prove which database the backup came from. That's your attestation in Step 3.
- If anything wrote to the old books after Step 1 (a program you missed), that write is not in the new books. Step 1's checks are there to prevent this; they can't detect a program that starts again later.
