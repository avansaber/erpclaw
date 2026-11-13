// The supervisor: INSTALLER 2.3 items 1, 2, 4, 5, 7 and SERVICE_COMMANDS r1b section 2's orders.
using ErpClaw.Service.Core;
using Xunit;

namespace ErpClaw.Service.Core.Tests;

public class SupervisorStartTests
{
    [Fact]
    public async Task Start_order_is_pointer_then_postgres_then_update_step_then_serve()
    {
        var rig = new Rig();
        await rig.StartAndWaitRunning();

        var order = rig.Journal.Only(0, "run pointer", "config", "start postgres", "probe", "books?", "run update-step",
            "start engine", "run file?");
        Assert.Equal(new[] { "run pointer", "config", "start postgres", "probe", "books?", "run update-step", "start engine", "run file?" },
            order);
        Assert.Equal(0, rig.Journal.Count("run bootstrap"));
    }

    [Fact]
    public async Task Waits_until_the_owner_login_succeeds_before_anything_uses_the_database()
    {
        var rig = new Rig();
        foreach (var answer in new[] { false, false, false }) rig.Readiness.Answers.Enqueue(answer);
        await rig.StartAndWaitRunning();

        Assert.Equal(4, rig.Journal.Count("probe"));
        Assert.True(rig.Journal.LastIndexOf("probe") < rig.Journal.IndexOf("books?"));
        Assert.Equal(new[] { 0.5, 0.5, 0.5 }, rig.Scheduler.Delays.Select(d => d.TotalSeconds));
        Assert.All(rig.Readiness.PortsProbed, p => Assert.Equal(rig.Config.Ports.Last(), p));
    }

    [Fact]
    public async Task The_record_holds_the_port_before_the_first_login_probe_and_the_children_with_start_times()
    {
        var rig = new Rig();
        await rig.StartAndWaitRunning();

        var record = rig.Records.Current!;
        Assert.Equal(ServiceState.Running, record.State);
        Assert.Equal(rig.Config.Ports.Last(), record.DatabasePort);
        Assert.Equal(new ChildRecord(rig.Postgres.Id, rig.Postgres.StartTime), record.Postgres);
        Assert.Equal(new ChildRecord(rig.Engine.Id, rig.Engine.StartTime), record.Engine);
        // Written with the port before PostgreSQL was probed (the engine's commands read it there).
        Assert.Contains(rig.Records.Written, r => r.DatabasePort == rig.Config.Ports.Last() && r.Postgres is not null && r.Engine is null);
    }

    [Fact]
    public async Task Postgres_that_never_accepts_a_login_counts_as_a_failure_and_is_killed()
    {
        var rig = new Rig { Options = new SupervisorOptions { ReadinessTimeout = TimeSpan.FromSeconds(2) } };
        rig.Readiness.Default = false;
        rig.Start();
        await rig.WaitState(ServiceState.GaveUp);

        Assert.Equal(5, rig.Launcher.Named("postgres").Count);
        Assert.All(rig.Launcher.Named("postgres"), p => Assert.False(p.Running));
        Assert.Equal(0, rig.Journal.Count("start engine"));
        Assert.Contains("database did not start", rig.Supervisor.Reason);
    }

    [Fact]
    public async Task Bootstraps_when_no_books_exist_before_the_update_step_and_the_engine()
    {
        var rig = new Rig();
        rig.Books.State = BooksState.Absent;
        rig.Runner.Script = (name, _) =>
        {
            if (name == "bootstrap") rig.Books.State = BooksState.Present;
            return null;
        };
        await rig.StartAndWaitRunning();

        Assert.Equal(new[] { "books?", "run bootstrap", "run update-step", "start engine" },
            rig.Journal.Only(0, "books?", "run bootstrap", "run update-step", "start engine"));
    }

    [Fact]
    public async Task A_bootstrap_crash_before_its_commit_bootstraps_again_and_no_engine_runs_on_empty_books()
    {
        var rig = new Rig();
        rig.Books.State = BooksState.Absent;
        int runs = 0;
        rig.Runner.Script = (name, _) =>
        {
            if (name != "bootstrap") return null;
            if (++runs == 1) return Outcome.Of(1, ""); // crashed before its commit: still no books
            rig.Books.State = BooksState.Present;
            return null;
        };
        await rig.StartAndWaitRunning();

        Assert.Equal(2, rig.Journal.Count("run bootstrap"));
        Assert.Single(rig.Launcher.Named("engine"));
        Assert.True(rig.Journal.IndexOf("start engine") > rig.Journal.LastIndexOf("run bootstrap"));
        Assert.Equal(new[] { 1.0 }, rig.Scheduler.BackoffSeconds);
    }

    [Fact]
    public async Task A_bootstrap_crash_after_its_commit_leaves_the_books_untouched_on_the_next_try()
    {
        var rig = new Rig();
        rig.Books.State = BooksState.Absent;
        rig.Runner.Script = (name, _) =>
        {
            if (name != "bootstrap") return null;
            rig.Books.State = BooksState.Present; // committed, then the process died
            return Outcome.Of(1, "");
        };
        await rig.StartAndWaitRunning();

        Assert.Equal(1, rig.Journal.Count("run bootstrap"));
        Assert.Equal(2, rig.Journal.Count("books?"));
        Assert.Single(rig.Launcher.Named("engine"));
    }

    [Fact]
    public async Task Books_that_cannot_be_checked_are_a_failure_not_an_empty_database()
    {
        var rig = new Rig();
        rig.Books.State = BooksState.Unknown;
        rig.Start();
        await rig.WaitState(ServiceState.GaveUp);

        Assert.Equal(0, rig.Journal.Count("run bootstrap"));
        Assert.Equal(0, rig.Journal.Count("start engine"));
    }

    [Theory]
    [InlineData(2)]
    [InlineData(4)]
    public async Task An_update_step_that_is_refused_or_rolled_back_stops_at_once_with_its_plain_text(int exit)
    {
        var rig = new Rig();
        rig.Runner.Script = (name, _) => name == "update-step" ? Outcome.ForExit(exit) : null;
        rig.Start();
        await rig.WaitState(ServiceState.GaveUp);

        Assert.Equal(Outcome.ForExit(exit).Text, rig.Supervisor.Reason);
        Assert.Equal(1, rig.Journal.Count("run update-step"));
        Assert.Equal(0, rig.Journal.Count("start engine"));
        Assert.Empty(rig.Scheduler.BackoffSeconds);
        Assert.Equal(ServiceState.GaveUp, rig.Records.Current!.State);
        Assert.Equal(rig.Supervisor.Reason, rig.Records.Current!.Reason);
        Assert.False(rig.Postgres.Running);
    }

    [Theory]
    [InlineData(1)]
    [InlineData(3)]
    public async Task An_update_step_that_could_not_run_is_tried_again_after_the_back_off(int exit)
    {
        var rig = new Rig();
        int runs = 0;
        rig.Runner.Script = (name, _) => name == "update-step" && ++runs == 1 ? Outcome.ForExit(exit) : null;
        await rig.StartAndWaitRunning();

        Assert.Equal(2, rig.Journal.Count("run update-step"));
        Assert.Equal(new[] { 1.0 }, rig.Scheduler.BackoffSeconds);
        Assert.Single(rig.Launcher.Named("engine"));
    }

    [Fact]
    public async Task A_pointer_naming_no_books_yet_is_a_fresh_install_and_goes_on()
    {
        var rig = new Rig();
        rig.Books.State = BooksState.Absent;
        rig.Runner.Script = (name, _) =>
        {
            if (name == "pointer") return Outcome.Of(2, """{"reason":"no_pointer","text":"No books yet."}""");
            if (name == "bootstrap") rig.Books.State = BooksState.Present;
            return null;
        };
        await rig.StartAndWaitRunning();

        Assert.Equal(1, rig.Journal.Count("run bootstrap"));
    }

    [Fact]
    public async Task A_pointer_refused_for_another_reason_stops_before_postgres_starts()
    {
        var rig = new Rig();
        rig.Runner.Script = (name, _) =>
            name == "pointer" ? Outcome.Of(2, """{"reason":"pointer_unreadable","text":"Your books' pointer cannot be read."}""") : null;
        rig.Start();
        await rig.WaitState(ServiceState.GaveUp);

        Assert.Equal("Your books' pointer cannot be read.", rig.Supervisor.Reason);
        Assert.Empty(rig.Launcher.Named("postgres"));
    }
}

public class SupervisorRestartTests
{
    [Fact]
    public async Task When_the_engine_exits_only_the_engine_restarts()
    {
        var rig = new Rig();
        await rig.StartAndWaitRunning();
        var postgres = rig.Postgres;
        int mark = rig.Journal.Entries.Count;

        rig.Engine.Exit(1);
        await rig.WaitEngineStarts(2);

        Assert.Single(rig.Launcher.Named("postgres"));
        Assert.True(postgres.Running);
        Assert.Equal(new[] { 1.0 }, rig.Scheduler.BackoffSeconds);
        Assert.Equal(new[] { "start engine", "run file?" },
            rig.Journal.Only(mark, "start engine", "run file?", "start postgres", "run update-step", "run pointer"));
    }

    [Fact]
    public async Task When_postgres_exits_the_engine_stops_first_then_postgres_then_the_engine_restart_together()
    {
        var rig = new Rig();
        await rig.StartAndWaitRunning();
        int port = rig.Config.Ports.Last();
        var engine = rig.Engine;
        int mark = rig.Journal.Entries.Count;

        rig.Postgres.Exit(1);
        await rig.WaitEngineStarts(2);

        Assert.False(engine.Running);
        Assert.Equal(new[] { "exit postgres", "exit engine", "config", "start postgres", "probe", "start engine" },
            rig.Journal.Only(mark, "exit postgres", "exit engine", "config", "start postgres", "probe", "start engine"));
        Assert.Equal(port, rig.Config.Ports.Last()); // the old port was still free
        Assert.Equal(new[] { 1.0 }, rig.Scheduler.BackoffSeconds);
        // The update step and the pointer run once per service start, not per PostgreSQL restart.
        Assert.Equal(1, rig.Journal.Count("run update-step"));
        Assert.Equal(1, rig.Journal.Count("run pointer"));
    }

    [Fact]
    public async Task A_new_port_is_drawn_only_when_the_old_one_is_taken_and_the_engine_starts_with_it()
    {
        var rig = new Rig();
        await rig.StartAndWaitRunning();
        int old = rig.Config.Ports.Last();
        rig.Ports.Taken.Add(old);

        rig.Postgres.Exit(1);
        await rig.WaitEngineStarts(2);

        int now = rig.Config.Ports.Last();
        Assert.NotEqual(old, now);
        Assert.Equal(now, rig.Readiness.PortsProbed.Last());
        // The record (where the engine's commands find the address) had the new port before the engine started.
        var atEngineStart = rig.Records.Written.Last(r => r.Engine is not null);
        Assert.Equal(now, atEngineStart.DatabasePort);
    }

    [Fact]
    public async Task An_engine_that_writes_no_run_file_is_killed_and_tried_again()
    {
        var rig = new Rig();
        rig.RunFiles.FailNext = 1;
        await rig.StartAndWaitRunning();

        var engines = rig.Launcher.Named("engine");
        Assert.Equal(2, engines.Count);
        Assert.False(engines[0].Running);
        Assert.True(engines[1].Running);
    }

    [Fact]
    public async Task Back_off_doubles_from_one_second_to_sixty_and_failures_spread_out_never_give_up()
    {
        var rig = new Rig();
        await rig.StartAndWaitRunning();
        for (int i = 1; i <= 8; i++)
        {
            rig.Clock.Advance(TimeSpan.FromMinutes(3));
            rig.Engine.Exit(1);
            await rig.WaitEngineStarts(i + 1);
        }

        Assert.Equal(new[] { 1.0, 2, 4, 8, 16, 32, 60, 60 }, rig.Scheduler.BackoffSeconds);
        Assert.Equal(ServiceState.Running, rig.Supervisor.State);
    }

    [Fact]
    public async Task A_long_stable_run_resets_the_back_off()
    {
        var rig = new Rig();
        await rig.StartAndWaitRunning();
        rig.Engine.Exit(1);
        await rig.WaitEngineStarts(2);
        rig.Engine.Exit(1);
        await rig.WaitEngineStarts(3);
        rig.Clock.Advance(TimeSpan.FromMinutes(11));
        rig.Engine.Exit(1);
        await rig.WaitEngineStarts(4);

        Assert.Equal(new[] { 1.0, 2, 1 }, rig.Scheduler.BackoffSeconds);
    }

    [Fact]
    public async Task Five_failures_in_ten_minutes_stop_the_tries_and_record_the_plain_reason()
    {
        var rig = new Rig();
        rig.RunFiles.AlwaysFail = true;
        rig.Start();
        await rig.WaitState(ServiceState.GaveUp);

        Assert.Equal(5, rig.Launcher.Named("engine").Count);
        Assert.Equal(new[] { 1.0, 2, 4, 8 }, rig.Scheduler.BackoffSeconds);
        var reason = rig.Supervisor.Reason!;
        Assert.Contains("five failures in ten minutes", reason);
        Assert.DoesNotMatch(@"\d{3,}", reason); // no port, no PID
        Assert.Equal(ServiceState.GaveUp, rig.Records.Current!.State);
        Assert.Equal(reason, rig.Records.Current!.Reason);
        // Everything was stopped: the engine, then PostgreSQL by pg_ctl.
        Assert.All(rig.Launcher.Started.Where(c => c.Name is "engine" or "postgres"), c => Assert.False(c.Running));
        Assert.Equal(1, rig.Journal.Count("start pg_ctl"));
        var status = await rig.Ask(ControlOps.Status);
        Assert.Equal("gave_up", status.State);
        Assert.Equal(reason, status.Reason);
    }

    [Fact]
    public async Task Postgres_and_engine_failures_count_together()
    {
        var rig = new Rig();
        await rig.StartAndWaitRunning();
        rig.Engine.Exit(1);
        await rig.WaitEngineStarts(2);
        rig.Postgres.Exit(1);
        await rig.WaitEngineStarts(3);
        rig.Engine.Exit(1);
        await rig.WaitEngineStarts(4);
        rig.Postgres.Exit(1);
        await rig.WaitEngineStarts(5);
        rig.Engine.Exit(1);
        await rig.WaitState(ServiceState.GaveUp);

        Assert.Equal(new[] { 1.0, 2, 4, 8 }, rig.Scheduler.BackoffSeconds);
    }
}

public class SupervisorStopTests
{
    [Fact]
    public async Task A_clean_stop_ends_the_engine_then_stops_postgres_by_pg_ctl_and_waits_for_the_postmaster()
    {
        var rig = new Rig();
        await rig.StartAndWaitRunning();
        int mark = rig.Journal.Entries.Count;

        rig.Supervisor.RequestStop();
        await rig.Running!.WaitAsync(TimeSpan.FromSeconds(5));

        Assert.Equal(new[] { "exit engine", "start pg_ctl", "exit postgres" },
            rig.Journal.Only(mark, "exit engine", "start pg_ctl", "exit postgres", "kill postgres"));
        var pgctl = rig.Launcher.Latest("pg_ctl")!;
        Assert.Equal(new[] { "stop", "-D", rig.Commands.Paths.Books, "-m", "fast" }, pgctl.Spec.Arguments);
        Assert.Equal(ServiceState.Stopped, rig.Supervisor.State);
        var record = rig.Records.Current!;
        Assert.Equal(ServiceState.Stopped, record.State);
        Assert.Null(record.Postgres);
        Assert.Null(record.Engine);
    }

    [Fact]
    public async Task A_postmaster_that_outlives_its_stop_timeout_is_killed()
    {
        var rig = new Rig();
        rig.Launcher.PostmasterIgnoresStop = true;
        await rig.StartAndWaitRunning();

        rig.Supervisor.RequestStop();
        await rig.Running!.WaitAsync(TimeSpan.FromSeconds(5));

        Assert.True(rig.Journal.IndexOf("kill postgres") > rig.Journal.IndexOf("start pg_ctl"));
        Assert.Contains(rig.Options.StopTimeout, rig.Scheduler.Delays);
    }

    [Fact]
    public async Task Quit_on_the_control_pipe_answers_then_stops_cleanly()
    {
        var rig = new Rig();
        await rig.StartAndWaitRunning();

        var response = await rig.Ask(ControlOps.Quit);
        await rig.Running!.WaitAsync(TimeSpan.FromSeconds(5));

        Assert.True(response.Ok);
        Assert.Equal(ServiceState.Stopped, rig.Supervisor.State);
        Assert.True(rig.Journal.IndexOf("exit engine") < rig.Journal.IndexOf("start pg_ctl"));
    }

    [Fact]
    public async Task A_stop_after_giving_up_still_ends_the_run()
    {
        var rig = new Rig();
        rig.RunFiles.AlwaysFail = true;
        rig.Start();
        await rig.WaitState(ServiceState.GaveUp);

        rig.Supervisor.RequestStop();
        await rig.Running!.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Equal(ServiceState.Stopped, rig.Supervisor.State);
    }
}

public class SupervisorLeftoverTests
{
    [Fact]
    public async Task At_start_recorded_children_still_alive_with_their_start_times_are_stopped_before_anything_starts()
    {
        var rig = new Rig();
        var t = new DateTimeOffset(2026, 11, 1, 8, 0, 0, TimeSpan.Zero);
        rig.Records.Initial = new ServiceRecord
        {
            Postgres = new ChildRecord(777, t),
            Engine = new ChildRecord(778, t.AddSeconds(5)),
            DatabasePort = 50999,
            State = ServiceState.Running,
        };
        rig.Inspector.Alive[777] = t;
        rig.Inspector.Alive[778] = t.AddSeconds(5);
        await rig.StartAndWaitRunning();

        Assert.Equal(new[] { 778, 777 }, rig.Inspector.Killed);
        Assert.True(rig.Journal.IndexOf("kill leftover 777") < rig.Journal.IndexOf("start postgres"));
        Assert.True(rig.Journal.IndexOf("kill leftover 777") < rig.Journal.IndexOf("run pointer"));
    }

    [Fact]
    public async Task A_reused_pid_is_never_stopped()
    {
        var rig = new Rig();
        var t = new DateTimeOffset(2026, 11, 1, 8, 0, 0, TimeSpan.Zero);
        rig.Records.Initial = new ServiceRecord { Postgres = new ChildRecord(777, t) };
        rig.Inspector.Alive[777] = t.AddTicks(1);
        await rig.StartAndWaitRunning();

        Assert.Empty(rig.Inspector.Killed);
    }
}

public class SupervisorOperationTests
{
    [Fact]
    public async Task The_daily_backup_runs_with_the_engine_running()
    {
        var rig = new Rig();
        await rig.StartAndWaitRunning();
        var engine = rig.Engine;

        var outcome = await rig.Supervisor.RunDailyBackupAsync();

        Assert.NotNull(outcome);
        Assert.True(outcome!.Succeeded);
        Assert.Equal(1, rig.Journal.Count("run backup daily"));
        Assert.True(engine.Running);
        Assert.Equal(0, rig.Journal.Count("kill engine"));
        var spec = rig.Runner.Specs.Single(s => Names.Of(s) == "backup daily");
        Assert.Equal(rig.Commands.Backup(BackupKind.Daily).Arguments, spec.Arguments);
    }

    [Fact]
    public async Task The_daily_backup_is_skipped_when_the_service_is_not_running()
    {
        var rig = new Rig();
        rig.RunFiles.AlwaysFail = true;
        rig.Start();
        await rig.WaitState(ServiceState.GaveUp);

        Assert.Null(await rig.Supervisor.RunDailyBackupAsync());
        Assert.Equal(0, rig.Journal.Count("run backup daily"));
    }

    [Fact]
    public async Task Backup_now_runs_a_manual_backup_and_answers_its_object()
    {
        var rig = new Rig();
        rig.Runner.Script = (name, _) => name == "backup manual"
            ? Outcome.Of(0, """{"file":"b.zip","sha256":"ab","size":10,"kind":"manual","pause_ms":3,"copied":false}""")
            : null;
        await rig.StartAndWaitRunning();

        var response = await rig.Ask(ControlOps.BackupNow);

        Assert.True(response.Ok);
        Assert.Equal("b.zip", (string?)response.Result!["file"]);
        Assert.True(rig.Engine.Running);
    }

    [Theory]
    [InlineData(1, ControlErrors.Internal)]
    [InlineData(2, ControlErrors.Refused)]
    [InlineData(3, ControlErrors.CouldNotRun)]
    [InlineData(4, ControlErrors.FailedUnchanged)]
    public async Task Each_exit_code_of_a_command_is_a_plain_error(int exit, string error)
    {
        var rig = new Rig();
        rig.Runner.Script = (name, _) => name is "backup manual" or "list-backups" ? Outcome.ForExit(exit) : null;
        await rig.StartAndWaitRunning();

        foreach (var op in new[] { ControlOps.BackupNow, ControlOps.ListBackups })
        {
            var response = await rig.Ask(op);
            Assert.False(response.Ok);
            Assert.Equal(error, response.Error);
            Assert.Equal(Outcome.ForExit(exit).Text, response.Text);
            if (exit == 2) Assert.Equal("not_allowed", response.Reason);
        }
    }

    [Fact]
    public async Task List_backups_answers_the_command_object()
    {
        var rig = new Rig();
        rig.Runner.Script = (name, _) => name == "list-backups" ? Outcome.Of(0, """{"backups":[],"replaced":[]}""") : null;
        await rig.StartAndWaitRunning();

        var response = await rig.Ask(ControlOps.ListBackups);
        Assert.True(response.Ok);
        Assert.NotNull(response.Result!["replaced"]);
    }

    [Fact]
    public async Task Remove_replaced_runs_the_command_with_the_name()
    {
        var rig = new Rig();
        rig.Runner.Script = (name, _) => name == "remove-replaced" ? Outcome.Of(0, """{"removed":"erpclaw_old"}""") : null;
        await rig.StartAndWaitRunning();

        var response = await rig.Ask(ControlOps.RemoveReplaced, "erpclaw_old");

        Assert.True(response.Ok);
        Assert.Equal(rig.Commands.RemoveReplaced("erpclaw_old").Arguments, rig.Runner.Specs.Last().Arguments);
    }

    [Fact]
    public async Task Remove_replaced_partial_is_resumed_by_running_it_again_after_the_back_off()
    {
        var rig = new Rig();
        int runs = 0;
        rig.Runner.Script = (name, _) => name != "remove-replaced" ? null
            : ++runs < 3 ? Outcome.Of(4, """{"reason":"partial","partial":["database"],"text":"Part of the old books was removed."}""")
            : Outcome.Of(0, """{"removed":"erpclaw_old"}""");
        await rig.StartAndWaitRunning();

        var response = await rig.Ask(ControlOps.RemoveReplaced, "erpclaw_old");

        Assert.True(response.Ok);
        Assert.Equal(3, rig.Journal.Count("run remove-replaced"));
        Assert.Equal(new[] { 1.0, 2 }, rig.Scheduler.BackoffSeconds);
        Assert.Equal(ServiceState.Running, rig.Supervisor.State); // these are not service failures
    }

    [Fact]
    public async Task Remove_replaced_still_partial_after_its_tries_reports_the_plain_sentence()
    {
        var rig = new Rig();
        rig.Runner.Script = (name, _) => name == "remove-replaced"
            ? Outcome.Of(4, """{"reason":"partial","partial":["database","files"],"text":"Part of the old books was removed."}""")
            : null;
        await rig.StartAndWaitRunning();

        var response = await rig.Ask(ControlOps.RemoveReplaced, "erpclaw_old");

        Assert.False(response.Ok);
        Assert.Equal(ControlErrors.FailedUnchanged, response.Error);
        Assert.Equal("Part of the old books was removed.", response.Text);
        Assert.Equal(rig.Options.RemoveReplacedAttempts, rig.Journal.Count("run remove-replaced"));
    }

    [Fact]
    public async Task Remove_replaced_exit_4_without_partial_is_not_retried()
    {
        var rig = new Rig();
        rig.Runner.Script = (name, _) => name == "remove-replaced" ? Outcome.ForExit(4) : null;
        await rig.StartAndWaitRunning();

        var response = await rig.Ask(ControlOps.RemoveReplaced, "erpclaw_old");

        Assert.False(response.Ok);
        Assert.Equal(1, rig.Journal.Count("run remove-replaced"));
    }

    [Fact]
    public async Task Mint_code_while_running_answers_the_minted_code()
    {
        var rig = new Rig();
        await rig.StartAndWaitRunning();

        var response = await rig.Ask(ControlOps.MintCode, "owner");

        Assert.True(response.Ok);
        Assert.Equal("ABCDEFGHIJKLMNOPQRSTUVWXYZ", response.Code!.Reveal());
        Assert.Equal(new[] { "owner" }, rig.Minter.Principals);
        Assert.Equal(new[] { rig.RunFiles.Last }, rig.Minter.RunIds);
        Assert.DoesNotContain("ABCDEFGHIJKLMNOPQRSTUVWXYZ", string.Join("\n", rig.Sink.Lines));
    }

    [Fact]
    public async Task Mint_code_for_a_run_the_host_did_not_verify_is_refused_and_mints_nothing()
    {
        var rig = new Rig();
        await rig.StartAndWaitRunning();
        var verified = rig.RunFiles.Last!;
        rig.Engine.Exit(1); // the engine restarts into a new run after the host verified the old one
        await rig.WaitEngineStarts(2);
        Assert.NotEqual(verified, rig.RunFiles.Last);

        var response = await rig.Mint("owner", verified);

        Assert.False(response.Ok);
        Assert.Equal(ControlErrors.StaleRun, response.Error);
        Assert.False(string.IsNullOrWhiteSpace(response.Text));
        Assert.Null(response.Code);
        Assert.Empty(rig.Minter.Principals);
        Assert.True((await rig.Mint("owner", rig.RunFiles.Last!)).Ok);
    }

    [Fact]
    public async Task Mint_code_for_an_unknown_principal_gives_no_code()
    {
        var rig = new Rig();
        rig.Minter.Next = new MintResult(MintStatus.UnknownPrincipal, null, "There is no one by that name in these books.");
        await rig.StartAndWaitRunning();

        var response = await rig.Ask(ControlOps.MintCode, "stranger");

        Assert.False(response.Ok);
        Assert.Equal(ControlErrors.UnknownPrincipal, response.Error);
        Assert.Null(response.Code);
    }

    [Fact]
    public async Task Mint_code_when_not_running_is_unavailable()
    {
        var rig = new Rig();
        rig.RunFiles.AlwaysFail = true;
        rig.Start();
        await rig.WaitState(ServiceState.GaveUp);

        var response = await rig.Ask(ControlOps.MintCode, "owner");

        Assert.False(response.Ok);
        Assert.Equal(ControlErrors.Unavailable, response.Error);
        Assert.Empty(rig.Minter.Principals);
    }

    [Fact]
    public async Task Status_while_running()
    {
        var rig = new Rig();
        await rig.StartAndWaitRunning();
        var response = await rig.Ask(ControlOps.Status);
        Assert.True(response.Ok);
        Assert.Equal("running", response.State);
        Assert.Null(response.Reason);
    }
}

public class SupervisorRestoreTests
{
    private static async Task<Rig> Running(Func<string, ProcessSpec, CommandOutcome?>? script = null)
    {
        var rig = new Rig { Runner = { Script = script } };
        await rig.StartAndWaitRunning();
        return rig;
    }

    [Fact]
    public async Task Restore_stages_then_stops_the_engine_then_switches_then_starts_the_engine()
    {
        var rig = await Running((name, _) => name == "switch" ? Outcome.Of(0, """{"database":"erpclaw_r2","previous":["erpclaw"]}""") : null);
        var oldEngine = rig.Engine;
        int mark = rig.Journal.Entries.Count;

        var response = await rig.Ask(ControlOps.Restore, "erpclaw-2026-11-01.zip");

        Assert.True(response.Ok);
        Assert.Equal(new[] { "run restore-stage", "exit engine", "run switch", "start engine", "run file?" },
            rig.Journal.Only(mark, "run restore-stage", "exit engine", "run switch", "start engine", "run file?", "start postgres"));
        Assert.Equal(rig.Commands.RestoreStage("erpclaw-2026-11-01.zip").Arguments,
            rig.Runner.Specs.Single(s => Names.Of(s) == "restore-stage").Arguments);
        Assert.Equal(rig.Commands.Switch("erpclaw_r2").Arguments, rig.Runner.Specs.Single(s => Names.Of(s) == "switch").Arguments);
        Assert.False(oldEngine.Running);
        Assert.True(rig.Engine.Running);
        Assert.Equal("erpclaw_r2", (string?)response.Result!["database"]);
        Assert.Empty(rig.Scheduler.BackoffSeconds); // the planned stop is not a failure
        Assert.Equal(ServiceState.Running, rig.Supervisor.State);
    }

    [Theory]
    [InlineData(1, ControlErrors.Internal)]
    [InlineData(2, ControlErrors.Refused)]
    [InlineData(3, ControlErrors.CouldNotRun)]
    [InlineData(4, ControlErrors.FailedUnchanged)]
    public async Task A_failed_stage_leaves_the_engine_running_on_the_old_books(int exit, string error)
    {
        var rig = await Running((name, _) => name == "restore-stage" ? Outcome.ForExit(exit) : null);
        var engine = rig.Engine;

        var response = await rig.Ask(ControlOps.Restore, "b.zip");

        Assert.False(response.Ok);
        Assert.Equal(error, response.Error);
        Assert.True(engine.Running);
        Assert.Equal(0, rig.Journal.Count("kill engine"));
        Assert.Equal(0, rig.Journal.Count("run switch"));
    }

    [Fact]
    public async Task A_stage_that_names_no_staged_database_is_an_internal_error_and_nothing_switches()
    {
        var rig = await Running((name, _) => name == "restore-stage" ? Outcome.Of(0, """{"proof":"ok"}""") : null);

        var response = await rig.Ask(ControlOps.Restore, "b.zip");

        Assert.False(response.Ok);
        Assert.Equal(ControlErrors.Internal, response.Error);
        Assert.Equal(0, rig.Journal.Count("kill engine"));
        Assert.Equal(0, rig.Journal.Count("run switch"));
    }

    [Theory]
    [InlineData(1, ControlErrors.Internal)]
    [InlineData(2, ControlErrors.Refused)]
    [InlineData(3, ControlErrors.CouldNotRun)]
    [InlineData(4, ControlErrors.FailedUnchanged)]
    public async Task A_failed_switch_restarts_the_engine_on_the_old_books(int exit, string error)
    {
        var rig = await Running((name, _) => name == "switch" ? Outcome.ForExit(exit) : null);
        int mark = rig.Journal.Entries.Count;

        var response = await rig.Ask(ControlOps.Restore, "b.zip");

        Assert.False(response.Ok);
        Assert.Equal(error, response.Error);
        Assert.Equal(new[] { "run restore-stage", "exit engine", "run switch", "start engine" },
            rig.Journal.Only(mark, "run restore-stage", "exit engine", "run switch", "start engine"));
        Assert.True(rig.Engine.Running);
        Assert.Equal(ServiceState.Running, rig.Supervisor.State);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task A_thrown_switch_launch_error_recovers_or_gives_up_cleanly(bool recoveryFails)
    {
        var rig = await Running((name, _) => name == "switch"
            ? throw new System.ComponentModel.Win32Exception(2, "private launch details") : null);
        var oldEngine = rig.Engine;
        rig.RunFiles.AlwaysFail = recoveryFails;
        var response = await rig.Ask(ControlOps.Restore, "b.zip");
        Assert.False(response.Ok);
        Assert.Equal(ControlErrors.Internal, response.Error);
        Assert.DoesNotContain("private", response.Text);
        Assert.False(oldEngine.Running);
        Assert.Equal(recoveryFails ? ServiceState.GaveUp : ServiceState.Running, rig.Supervisor.State);
        Assert.Equal(rig.Supervisor.State, rig.Records.Current!.State);
        Assert.Equal(!recoveryFails, rig.Engine.Running);
        if (recoveryFails) Assert.False(rig.Postgres.Running);
        Assert.DoesNotContain(rig.Sink.Lines, line => line.Contains("private", StringComparison.Ordinal));
        rig.Supervisor.RequestStop();
        await rig.Running!.WaitAsync(TimeSpan.FromSeconds(5));
    }

    [Fact]
    public async Task A_thrown_engine_launch_error_during_restore_enters_a_clean_failure_state()
    {
        var rig = await Running();
        rig.Launcher.OnStart = child =>
        {
            if (child.Name == "engine")
            {
                child.Exit(1);
                throw new IOException("private launch details");
            }
        };
        await Assert.ThrowsAsync<IOException>(() => rig.Ask(ControlOps.Restore, "b.zip"));
        await rig.WaitState(ServiceState.GaveUp);
        Assert.False(rig.Postgres.Running);
        Assert.False(rig.Engine.Running);
        rig.Supervisor.RequestStop();
        await rig.Running!.WaitAsync(TimeSpan.FromSeconds(5));
    }

    [Fact]
    public async Task A_thrown_launch_error_during_recovery_cleanup_still_gives_up()
    {
        var rig = await Running();
        rig.Launcher.PostmasterIgnoresStop = true;
        rig.Launcher.OnStart = child =>
        {
            if (child.Name is "engine" or "pg_ctl")
            {
                child.Exit(1);
                throw new IOException("private launch details");
            }
        };
        await Assert.ThrowsAsync<IOException>(() => rig.Ask(ControlOps.Restore, "b.zip"));
        await rig.WaitState(ServiceState.GaveUp);
        Assert.False(rig.Postgres.Running);
        Assert.False(rig.Engine.Running);
        rig.Supervisor.RequestStop();
        await rig.Running!.WaitAsync(TimeSpan.FromSeconds(5));
    }

    [Fact]
    public async Task An_engine_that_does_not_start_after_the_switch_is_tried_again_with_the_back_off()
    {
        var rig = await Running();
        rig.RunFiles.FailNext = 1;

        var response = await rig.Ask(ControlOps.Restore, "b.zip");

        Assert.True(response.Ok);
        Assert.Equal(new[] { 1.0 }, rig.Scheduler.BackoffSeconds);
        Assert.True(rig.Engine.Running);
    }

    [Fact]
    public async Task An_engine_that_never_starts_after_the_switch_is_reported_with_the_plain_reason()
    {
        var rig = await Running();
        rig.RunFiles.AlwaysFail = true;

        var response = await rig.Ask(ControlOps.Restore, "b.zip");

        Assert.False(response.Ok);
        Assert.Equal(ControlErrors.NotRestarted, response.Error);
        Assert.Equal(ServiceState.GaveUp, rig.Supervisor.State);
        Assert.Equal(rig.Supervisor.Reason, response.Text);
    }

    [Fact]
    public async Task Restore_when_not_running_is_unavailable_and_runs_nothing()
    {
        var rig = new Rig();
        rig.RunFiles.AlwaysFail = true;
        rig.Start();
        await rig.WaitState(ServiceState.GaveUp);

        var response = await rig.Ask(ControlOps.Restore, "b.zip");

        Assert.False(response.Ok);
        Assert.Equal(ControlErrors.Unavailable, response.Error);
        Assert.Equal(0, rig.Journal.Count("run restore-stage"));
    }

    [Fact]
    public async Task Mint_during_a_restore_is_unavailable()
    {
        Rig? rigRef = null;
        ControlResponse? during = null;
        var rig = await Running((name, _) =>
        {
            if (name == "switch") during = rigRef!.Ask(ControlOps.MintCode, "owner").GetAwaiter().GetResult();
            return null;
        });
        rigRef = rig;

        await rig.Ask(ControlOps.Restore, "b.zip");

        Assert.NotNull(during);
        Assert.False(during!.Ok);
        Assert.Equal(ControlErrors.Unavailable, during.Error);
    }
}

public class DailyBackupScheduleTests
{
    private static readonly DailyBackupOptions Options = new();

    private static (FakeClock clock, DailyBackupSchedule schedule) Schedule(DateOnly? lastDaily = null)
    {
        var clock = new FakeClock(); // 2026-11-02 09:00 UTC
        return (clock, new DailyBackupSchedule(clock, TimeZoneInfo.Utc, lastDaily, Options));
    }

    [Fact]
    public void Nothing_is_due_until_the_engine_is_ready_then_ten_minutes_later()
    {
        var (clock, schedule) = Schedule();
        Assert.Null(schedule.NextDue);
        Assert.False(schedule.IsDue);

        schedule.EngineReady();
        Assert.Equal(clock.Now + TimeSpan.FromMinutes(10), schedule.NextDue);
        clock.Advance(TimeSpan.FromMinutes(9));
        Assert.False(schedule.IsDue);
        clock.Advance(TimeSpan.FromMinutes(1));
        Assert.True(schedule.IsDue);
    }

    [Fact]
    public void After_a_good_backup_the_next_is_the_next_calendar_day_once_the_engine_has_been_ready_ten_minutes()
    {
        var (clock, schedule) = Schedule();
        schedule.EngineReady();
        clock.Advance(TimeSpan.FromMinutes(10));
        schedule.Record(Outcome.Of(0, "{}"));

        Assert.False(schedule.IsDue);
        Assert.Equal(new DateTimeOffset(2026, 11, 3, 0, 0, 0, TimeSpan.Zero), schedule.NextDue);
        schedule.EngineReady(); // a restart the same day takes no second daily backup
        Assert.Equal(new DateTimeOffset(2026, 11, 3, 0, 0, 0, TimeSpan.Zero), schedule.NextDue);

        clock.Advance(TimeSpan.FromHours(15)); // 2026-11-03 00:10
        Assert.True(schedule.IsDue);
    }

    [Fact]
    public void A_daily_backup_already_taken_today_is_not_taken_again()
    {
        var (clock, schedule) = Schedule(new DateOnly(2026, 11, 2));
        schedule.EngineReady();
        clock.Advance(TimeSpan.FromMinutes(30));
        Assert.False(schedule.IsDue);
    }

    [Fact]
    public void Yesterdays_backup_does_not_count_for_today()
    {
        var (clock, schedule) = Schedule(new DateOnly(2026, 11, 1));
        schedule.EngineReady();
        clock.Advance(TimeSpan.FromMinutes(10));
        Assert.True(schedule.IsDue);
    }

    [Fact]
    public void A_backup_that_could_not_run_is_tried_again_later_up_to_the_bound_then_the_next_day()
    {
        var (clock, schedule) = Schedule();
        schedule.EngineReady();
        clock.Advance(TimeSpan.FromMinutes(10));

        for (int i = 0; i < Options.RetriesPerDay; i++)
        {
            schedule.Record(Outcome.ForExit(3));
            Assert.Equal(clock.Now + Options.RetryAfter, schedule.NextDue);
            clock.Advance(Options.RetryAfter);
            Assert.True(schedule.IsDue);
        }
        schedule.Record(Outcome.ForExit(3));
        Assert.Equal(new DateTimeOffset(2026, 11, 3, 0, 0, 0, TimeSpan.Zero), schedule.NextDue);

        // The next day starts with its full tries again.
        clock.Advance(TimeSpan.FromDays(1));
        Assert.True(schedule.IsDue);
        schedule.Record(Outcome.ForExit(3));
        Assert.Equal(clock.Now + Options.RetryAfter, schedule.NextDue);
    }

    [Theory]
    [InlineData(1)]
    [InlineData(2)]
    [InlineData(4)]
    public void Other_failures_wait_for_the_next_day(int exit)
    {
        var (clock, schedule) = Schedule();
        schedule.EngineReady();
        clock.Advance(TimeSpan.FromMinutes(10));
        schedule.Record(Outcome.ForExit(exit));
        Assert.Equal(new DateTimeOffset(2026, 11, 3, 0, 0, 0, TimeSpan.Zero), schedule.NextDue);
    }

    [Fact]
    public void A_skipped_backup_with_the_engine_down_is_tried_again_later()
    {
        var (clock, schedule) = Schedule();
        schedule.EngineReady();
        clock.Advance(TimeSpan.FromMinutes(10));
        schedule.Record(null);
        Assert.Equal(clock.Now + Options.RetryAfter, schedule.NextDue);
    }

    [Fact]
    public void The_calendar_day_is_the_owners()
    {
        var clock = new FakeClock(); // 09:00 UTC is 2026-11-02 18:00 in UTC+9
        var zone = TimeZoneInfo.CreateCustomTimeZone("plus9", TimeSpan.FromHours(9), "plus9", "plus9");
        var schedule = new DailyBackupSchedule(clock, zone, null, Options);
        schedule.EngineReady();
        clock.Advance(TimeSpan.FromMinutes(10));
        schedule.Record(Outcome.Of(0, "{}"));
        Assert.Equal(new DateTimeOffset(2026, 11, 3, 0, 0, 0, TimeSpan.FromHours(9)), schedule.NextDue);
    }

    [Fact]
    public async Task The_supervisor_says_each_time_the_engine_is_ready()
    {
        var rig = new Rig();
        int ready = 0;
        rig.Supervisor.EngineReady += (_, _) => Interlocked.Increment(ref ready);
        await rig.StartAndWaitRunning();
        rig.Engine.Exit(1);
        await rig.WaitEngineStarts(2);
        await Eventually.True(() => Volatile.Read(ref ready) == 2, "two ready signals");
    }
}

public class SupervisorServiceFileTests
{
    [Fact]
    public async Task The_service_file_is_written_with_the_port_before_postgres_starts()
    {
        var rig = new Rig();
        await rig.StartAndWaitRunning();

        Assert.True(rig.Journal.IndexOf("service file write") >= 0);
        Assert.True(rig.Journal.IndexOf("service file write") < rig.Journal.IndexOf("start postgres"));
        var file = rig.ServiceFiles.Current!;
        Assert.Equal(rig.Session.ControlPipe, file.ControlPipe);
        Assert.Equal(rig.Session.Secret, file.ServiceSecret);
        Assert.Equal(rig.Config.Ports.Last().ToString(), file.DatabasePort);
    }

    [Fact]
    public async Task A_stale_file_from_a_crash_is_removed_before_the_new_one_is_written()
    {
        var rig = new Rig();
        await rig.StartAndWaitRunning();
        Assert.True(rig.Journal.IndexOf("service file delete") >= 0);
        Assert.True(rig.Journal.IndexOf("service file delete") < rig.Journal.IndexOf("service file write"));
    }

    [Fact]
    public async Task A_new_port_rewrites_the_file_with_the_same_pipe_and_secret()
    {
        var rig = new Rig();
        await rig.StartAndWaitRunning();
        int old = rig.Config.Ports.Last();
        rig.Ports.Taken.Add(old);

        rig.Postgres.Exit(1);
        await rig.WaitEngineStarts(2);

        var file = rig.ServiceFiles.Current!;
        Assert.Equal(rig.Config.Ports.Last().ToString(), file.DatabasePort);
        Assert.NotEqual(old.ToString(), file.DatabasePort);
        Assert.All(rig.ServiceFiles.Written, w =>
        {
            Assert.Equal(rig.Session.ControlPipe, w.ControlPipe);
            Assert.Equal(rig.Session.Secret, w.ServiceSecret);
        });
    }

    [Fact]
    public async Task The_service_file_is_deleted_when_the_service_stops_and_not_before()
    {
        var rig = new Rig();
        await rig.StartAndWaitRunning();
        int deletesBefore = rig.Journal.Count("service file delete");

        rig.Supervisor.RequestStop();
        await rig.Running!.WaitAsync(TimeSpan.FromSeconds(5));

        Assert.Equal(deletesBefore + 1, rig.Journal.Count("service file delete"));
        Assert.Null(rig.ServiceFiles.Current);
    }

    [Fact]
    public async Task Giving_up_leaves_the_file_so_the_host_can_still_ask_the_service_why()
    {
        var rig = new Rig();
        rig.Runner.Script = (name, _) => name == "update-step" ? Outcome.ForExit(2) : null;
        rig.Start();
        await rig.WaitState(ServiceState.GaveUp);
        Assert.NotNull(rig.ServiceFiles.Current);
    }
}

public class SupervisorFreshInstallTests
{
    private static Rig Fresh(Func<string, ProcessSpec, CommandOutcome?>? more = null)
    {
        var rig = new Rig();
        rig.Books.State = BooksState.Absent;
        rig.Runner.Script = (name, spec) =>
        {
            if (name == "pointer") return Outcome.Of(2, """{"reason":"no_pointer","text":"No books yet."}""");
            if (more?.Invoke(name, spec) is { } scripted) return scripted;
            if (name == "bootstrap") rig.Books.State = BooksState.Present;
            return null;
        };
        return rig;
    }

    [Fact]
    public async Task No_pointer_runs_init_own_books_then_switch_to_the_new_database_before_the_update_step()
    {
        var rig = Fresh();
        await rig.StartAndWaitRunning();

        Assert.Equal(new[] { "run pointer", "start postgres", "books?", "run bootstrap", "run switch", "run update-step", "start engine" },
            rig.Journal.Only(0, "run pointer", "start postgres", "books?", "run bootstrap", "run switch", "run update-step", "start engine"));
        var bootstrap = rig.Runner.Specs.Single(s => Names.Of(s) == "bootstrap");
        Assert.Equal(new[] { "init", "--own-books", "--data-folder", rig.Commands.Paths.DataFolder }, bootstrap.Arguments.TakeLast(4));
        var sw = rig.Runner.Specs.Single(s => Names.Of(s) == "switch");
        Assert.Equal(new[] { "service", "switch", "--data-folder", rig.Commands.Paths.DataFolder, "--to", "erpclaw" }, sw.Arguments.TakeLast(6));
    }

    [Fact]
    public async Task A_pointer_that_exists_never_runs_switch()
    {
        var rig = new Rig();
        await rig.StartAndWaitRunning();
        Assert.Equal(0, rig.Journal.Count("run switch"));
        Assert.Equal(0, rig.Journal.Count("run bootstrap"));
    }

    [Fact]
    public async Task Books_found_without_a_pointer_are_switched_to_without_running_init_again()
    {
        var rig = Fresh();
        rig.Books.State = BooksState.Present; // a crash after init, before switch
        await rig.StartAndWaitRunning();

        Assert.Equal(0, rig.Journal.Count("run bootstrap"));
        Assert.Equal(1, rig.Journal.Count("run switch"));
    }

    [Fact]
    public async Task A_switch_that_could_not_run_is_tried_again_and_init_does_not_run_twice()
    {
        int switches = 0;
        var rig = Fresh((name, _) => name == "switch" && ++switches == 1 ? Outcome.ForExit(3) : null);
        await rig.StartAndWaitRunning();

        Assert.Equal(1, rig.Journal.Count("run bootstrap"));
        Assert.Equal(2, rig.Journal.Count("run switch"));
        Assert.Equal(new[] { 1.0 }, rig.Scheduler.BackoffSeconds);
        Assert.Single(rig.Launcher.Named("engine"));
        Assert.Equal(1, rig.Journal.Count("run pointer"));
    }

    [Fact]
    public async Task A_switch_that_is_refused_stops_with_its_text_and_starts_no_engine()
    {
        var rig = Fresh((name, _) => name == "switch" ? Outcome.ForExit(2) : null);
        rig.Start();
        await rig.WaitState(ServiceState.GaveUp);

        Assert.Equal(Outcome.ForExit(2).Text, rig.Supervisor.Reason);
        Assert.Equal(1, rig.Journal.Count("run switch"));
        Assert.Equal(0, rig.Journal.Count("start engine"));
    }

    [Fact]
    public async Task A_failed_init_is_tried_again_and_switch_waits_for_it()
    {
        int inits = 0;
        var rig = Fresh((name, _) => name == "bootstrap" && ++inits == 1 ? Outcome.Of(1, "") : null);
        await rig.StartAndWaitRunning();

        Assert.Equal(2, rig.Journal.Count("run bootstrap"));
        Assert.Equal(1, rig.Journal.Count("run switch"));
        Assert.True(rig.Journal.IndexOf("run switch") > rig.Journal.LastIndexOf("run bootstrap"));
    }
}

public class SupervisorUpdateStepRetryTests
{
    [Theory]
    [InlineData(1)]
    [InlineData(3)]
    public async Task An_update_step_that_keeps_failing_is_retried_1_2_4_up_to_60_seconds_then_stops_with_its_text(int exit)
    {
        var rig = new Rig();
        rig.Runner.Script = (name, _) => name == "update-step" ? Outcome.ForExit(exit) : null;
        rig.Start();
        await rig.WaitState(ServiceState.GaveUp);

        Assert.Equal(new[] { 1.0, 2, 4, 8, 16, 32, 60 }, rig.Scheduler.BackoffSeconds);
        Assert.Equal(8, rig.Journal.Count("run update-step"));
        Assert.Equal(Outcome.ForExit(exit).Text, rig.Supervisor.Reason);
        Assert.Equal(0, rig.Journal.Count("start engine"));
        Assert.Equal(1, rig.Journal.Count("start postgres"));
        Assert.False(rig.Postgres.Running);
    }

    [Fact]
    public async Task An_update_step_that_succeeds_on_a_later_try_stops_the_back_off_there()
    {
        var rig = new Rig();
        int runs = 0;
        rig.Runner.Script = (name, _) => name == "update-step" && ++runs <= 3 ? Outcome.ForExit(3) : null;
        await rig.StartAndWaitRunning();

        Assert.Equal(new[] { 1.0, 2, 4 }, rig.Scheduler.BackoffSeconds);
        Assert.Equal(4, rig.Journal.Count("run update-step"));
        Assert.Single(rig.Launcher.Named("engine"));
    }

    [Fact]
    public async Task An_update_step_refused_after_a_retry_stops_at_once()
    {
        var rig = new Rig();
        int runs = 0;
        rig.Runner.Script = (name, _) => name == "update-step" ? Outcome.ForExit(++runs == 1 ? 3 : 4) : null;
        rig.Start();
        await rig.WaitState(ServiceState.GaveUp);

        Assert.Equal(new[] { 1.0 }, rig.Scheduler.BackoffSeconds);
        Assert.Equal(2, rig.Journal.Count("run update-step"));
        Assert.Equal(Outcome.ForExit(4).Text, rig.Supervisor.Reason);
    }
}

public class SupervisorEngineStopTests
{
    [Fact]
    public void The_stop_event_is_named_for_the_run_in_the_local_namespace()
    {
        Assert.Equal(@"Local\erpclaw-stop-0f1e2d3c", StopEvents.NameFor("0f1e2d3c"));
    }

    [Fact]
    public async Task A_clean_stop_signals_the_engine_s_event_and_does_not_kill_an_engine_that_exits()
    {
        var rig = new Rig();
        await rig.StartAndWaitRunning();
        var run = rig.RunFiles.Last!;
        int mark = rig.Journal.Entries.Count;

        rig.Supervisor.RequestStop();
        await rig.Running!.WaitAsync(TimeSpan.FromSeconds(5));

        Assert.Equal(new[] { StopEvents.NameFor(run) }, rig.StopSignal.Names);
        Assert.Equal(0, rig.Journal.Count("kill engine"));
        Assert.Equal(new[] { "signal", "exit engine", "start pg_ctl" }, rig.Journal.Only(mark, "signal", "exit engine", "start pg_ctl"));
        Assert.DoesNotContain(TimeSpan.FromSeconds(15), rig.Scheduler.Delays);
    }

    [Fact]
    public async Task An_engine_that_ignores_the_event_is_waited_for_15_seconds_and_then_killed()
    {
        var rig = new Rig();
        rig.StopSignal.EngineExits = false;
        await rig.StartAndWaitRunning();
        int mark = rig.Journal.Entries.Count;

        rig.Supervisor.RequestStop();
        await rig.Running!.WaitAsync(TimeSpan.FromSeconds(5));

        Assert.Equal(TimeSpan.FromSeconds(15), rig.Options.EngineStopTimeout);
        Assert.Contains(TimeSpan.FromSeconds(15), rig.Scheduler.Delays);
        Assert.Equal(new[] { "signal", "kill engine", "start pg_ctl" }, rig.Journal.Only(mark, "signal", "kill engine", "start pg_ctl"));
    }

    [Fact]
    public async Task An_event_that_cannot_be_opened_means_the_engine_is_killed_at_once()
    {
        var rig = new Rig();
        rig.StopSignal.CanOpen = false;
        await rig.StartAndWaitRunning();

        rig.Supervisor.RequestStop();
        await rig.Running!.WaitAsync(TimeSpan.FromSeconds(5));

        Assert.Equal(1, rig.Journal.Count("kill engine"));
        Assert.DoesNotContain(TimeSpan.FromSeconds(15), rig.Scheduler.Delays);
    }

    [Fact]
    public async Task An_engine_with_no_run_yet_has_no_event_and_is_killed_without_a_signal()
    {
        var rig = new Rig();
        rig.RunFiles.FailNext = 1;
        await rig.StartAndWaitRunning();

        Assert.Equal(1, rig.Journal.Count("kill engine"));
        Assert.Empty(rig.StopSignal.Names);
    }

    [Fact]
    public async Task A_restore_stops_the_engine_by_its_event_too()
    {
        var rig = new Rig();
        await rig.StartAndWaitRunning();
        var run = rig.RunFiles.Last!;

        var response = await rig.Ask(ControlOps.Restore, "erpclaw-2026-11-01.zip");

        Assert.True(response.Ok);
        Assert.Equal(StopEvents.NameFor(run), rig.StopSignal.Names.First());
        Assert.Equal(0, rig.Journal.Count("kill engine"));
    }
}

public class SupervisorMintLogTests
{
    [Fact]
    public async Task A_minted_code_is_logged_as_mint_code_ok_and_nothing_more()
    {
        var rig = new Rig();
        await rig.StartAndWaitRunning();

        var response = await rig.Ask(ControlOps.MintCode, "owner");

        Assert.True(response.Ok);
        var lines = rig.Sink.Lines.Where(l => l.Contains("mint-code")).ToArray();
        Assert.Single(lines);
        Assert.EndsWith(" mint-code ok", lines[0]);
        Assert.DoesNotContain(rig.Sink.Lines, l => l.Contains("ABCDEFGHIJKLMNOPQRSTUVWXYZ") || l.Contains(ServiceLog.WithheldNotice));
    }

    [Theory]
    [InlineData(MintStatus.Failed, 3)]
    [InlineData(MintStatus.UnknownPrincipal, 2)]
    public async Task A_mint_that_did_not_succeed_is_logged_as_its_exit_code(MintStatus status, int exit)
    {
        var rig = new Rig();
        rig.Minter.Next = new MintResult(status, null, "A plain sentence.", exit);
        await rig.StartAndWaitRunning();

        await rig.Ask(ControlOps.MintCode, "owner");

        Assert.Single(rig.Sink.Lines, l => l.EndsWith($" mint-code exit {exit}", StringComparison.Ordinal));
    }
}
