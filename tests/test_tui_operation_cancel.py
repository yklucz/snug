"""Operation cancellation preserves published work and returns to the menu."""

from contextlib import nullcontext
import hashlib
import os
from pathlib import Path
import signal
from types import SimpleNamespace
import zipfile

import pytest

import snug
import snug_core as core


CREATE_UNCHANGED = "No archive was created; any existing archive was left unchanged."
CREATE_PUBLISHED = "The current archive was kept; it may already have been published."
CREATE_UNKNOWN = "Archive state could not be verified; check the output path."
EXTRACT_CANCELLED = "Files already published were kept; unfinished staged files were removed where possible."
TEST_CANCELLED = "Test cancelled. No changes were made."


def interrupt(_value=None):
    raise KeyboardInterrupt


@pytest.fixture
def menu_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(snug, "_clear_screen", lambda: None)
    monkeypatch.setattr(snug, "_fullscreen", nullcontext)
    monkeypatch.setattr(snug, "_init_color", lambda: None)
    monkeypatch.setattr(snug, "_enable_ansi_windows", lambda: None)
    pauses = []
    monkeypatch.setattr(snug, "_wait_for_enter", lambda: pauses.append(True))
    return pauses


def native_engine():
    return core.ArchiveEngine([core.NativeBackend()])


def zip_inputs(tmp_path):
    archive = tmp_path / "input.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("first.txt", b"first complete payload")
        output.writestr("second.txt", b"second complete payload")
    return archive


def stop_engine_at(monkeypatch, engine, operation, event):
    original = getattr(engine, operation)

    def run(*args, **kwargs):
        monkeypatch.setattr(kwargs["progress"], event, interrupt)
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, operation, run)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("event", ["chunk", "done"])
def test_create_cancel_message_matches_publication_state(
        monkeypatch, tmp_path, menu_environment, capsys, existing, event):
    source = tmp_path / "source.txt"
    source.write_bytes(b"new archive payload")
    archive = tmp_path / "output.zip"
    previous = b"previous archive bytes"
    if existing:
        archive.write_bytes(previous)
    engine = native_engine()
    stop_engine_at(monkeypatch, engine, "create", event)

    report = snug._menu_run_operation(
        "create", lambda display: engine.create(archive, [source], progress=display),
        archive=archive,
    )

    assert report is None
    message = capsys.readouterr().out
    assert "Create cancelled." in message
    if event == "chunk":
        assert CREATE_UNCHANGED in message
        if existing:
            assert archive.read_bytes() == previous
        else:
            assert not archive.exists()
    else:
        assert CREATE_PUBLISHED in message
        assert CREATE_UNCHANGED not in message
        with zipfile.ZipFile(archive) as output:
            assert output.namelist() == [source.name]
            assert output.read(source.name) == b"new archive payload"
            assert output.testzip() is None
    assert not list(tmp_path.glob(f".{archive.name}.*.part"))


@pytest.mark.parametrize("phase", ["payload", "publication"])
def test_extract_cancel_retains_only_published_files_and_cleans_staging(
        monkeypatch, tmp_path, menu_environment, capsys, phase):
    archive = zip_inputs(tmp_path)
    destination = tmp_path / "destination"
    engine = native_engine()
    if phase == "payload":
        stop_engine_at(monkeypatch, engine, "extract", "chunk")
    else:
        original = core.SafeOutputFile.commit

        def stop_second(output):
            if output.entry.name == "second.txt":
                raise KeyboardInterrupt
            return original(output)

        monkeypatch.setattr(core.SafeOutputFile, "commit", stop_second)

    report = snug._menu_run_operation(
        "extract", lambda display: engine.extract(archive, destination, progress=display),
        archive=archive,
    )

    assert report is None
    assert EXTRACT_CANCELLED in capsys.readouterr().out
    assert not list(destination.glob(".snug-part-*"))
    assert sorted(path.name for path in destination.iterdir()) == (
        [] if phase == "payload" else ["first.txt"])
    if phase == "publication":
        assert (destination / "first.txt").read_bytes() == b"first complete payload"


def test_test_cancel_changes_no_archive_or_output(
        monkeypatch, tmp_path, menu_environment, capsys):
    archive = zip_inputs(tmp_path)
    previous_hash = hashlib.sha256(archive.read_bytes()).digest()
    engine = native_engine()
    stop_engine_at(monkeypatch, engine, "test", "chunk")

    report = snug._menu_run_operation(
        "test", lambda display: engine.test(archive, progress=display), archive=archive,
    )

    assert report is None
    assert TEST_CANCELLED in capsys.readouterr().out
    assert hashlib.sha256(archive.read_bytes()).digest() == previous_hash
    assert list(tmp_path.iterdir()) == [archive]


@pytest.mark.parametrize("operation,choice", [("create", "1"), ("extract", "2"), ("test", "5")])
def test_cancelled_real_engine_handler_returns_to_live_main_menu(
        monkeypatch, tmp_path, menu_environment, capsys, operation, choice):
    source = tmp_path / "source.txt"
    source.write_bytes(b"source payload")
    archive = zip_inputs(tmp_path)
    destination = tmp_path / "destination"
    output = tmp_path / "output.zip"
    engine = native_engine()
    stop_engine_at(monkeypatch, engine, operation, "chunk")
    monkeypatch.setattr(snug, "ArchiveEngine", lambda: engine)
    monkeypatch.setattr(snug, "_select_sources_arrow", lambda *_args: [source])
    monkeypatch.setattr(snug, "_select_archive", lambda: archive)

    def configure_create(options):
        options.fmt = core.ArchiveFormat.ZIP
        options.archive_path = output
        return True

    def configure_extract(_engine, options):
        options.destination = destination
        return True

    monkeypatch.setattr(snug, "_configure_create", configure_create)
    monkeypatch.setattr(snug, "_configure_extract", configure_extract)
    monkeypatch.setattr(snug, "_menu_confirm_overwrite", lambda _path: True)
    monkeypatch.setattr(snug, "_interactive_enumerate", core.ArchiveEngine._collect_items)
    main_choices = iter([choice, "0"])
    main_visits = []

    def select(title, _options, subtitle=None, **_kwargs):
        if title == "Select an action":
            main_visits.append(title)
            return next(main_choices)
        return "r"

    monkeypatch.setattr(snug, "_select_menu", select)

    assert snug._interactive_menu() == 0
    assert len(main_visits) == 2
    assert menu_environment == [True]
    message = capsys.readouterr().out
    assert "cancelled" in message.lower()
    assert "Archive is OK." not in message
    assert not output.exists()
    assert not list(tmp_path.rglob("*.part"))
    assert not list(tmp_path.rglob(".snug-part-*"))


@pytest.fixture
def operation_handlers(monkeypatch):
    def previous(_signum, _frame):
        pass

    numbers = [getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT")
               if hasattr(signal, name)]
    handlers = dict.fromkeys(numbers, previous)
    installed = []

    def install(signum, handler):
        installed.append(signum)
        old = handlers[signum]
        handlers[signum] = handler
        return old

    monkeypatch.setattr(signal, "getsignal", handlers.__getitem__)
    monkeypatch.setattr(signal, "signal", install)
    return SimpleNamespace(handlers=handlers, previous=previous, installed=installed)


@pytest.mark.parametrize("outcome", ["normal", "error", "interrupt", "close"])
def test_operation_signal_context_restores_all_handlers_on_every_exit(operation_handlers, outcome):
    context = snug._menu_operation_signals()
    if outcome == "close":
        context.__enter__()
        context.gen.close()
    elif outcome == "normal":
        with context:
            pass
    else:
        failure = RuntimeError("operation error") if outcome == "error" else KeyboardInterrupt()
        with pytest.raises(type(failure)):
            with context:
                raise failure
    assert all(handler is operation_handlers.previous for handler in operation_handlers.handlers.values())
    assert set(operation_handlers.installed) == {signal.SIGINT}


def test_second_ctrl_c_is_ignored_during_engine_cleanup(operation_handlers):
    cleanup_ran = []
    with pytest.raises(KeyboardInterrupt):
        with snug._menu_operation_signals():
            try:
                operation_handlers.handlers[signal.SIGINT](signal.SIGINT, None)
            finally:
                assert operation_handlers.handlers[signal.SIGINT] == signal.SIG_IGN
                cleanup_ran.append(True)
    assert cleanup_ran == [True]
    assert all(handler is operation_handlers.previous for handler in operation_handlers.handlers.values())
    assert set(operation_handlers.installed) == {signal.SIGINT}


@pytest.mark.parametrize("operation", ["create", "extract"])
def test_second_real_sigint_cannot_interrupt_engine_staging_cleanup(
        monkeypatch, tmp_path, menu_environment, capsys, operation):
    engine = native_engine()
    archive = tmp_path / "output.zip" if operation == "create" else zip_inputs(tmp_path)
    source = tmp_path / "source.txt"
    source.write_bytes(b"source payload")
    destination = tmp_path / "destination"
    numbers = [getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGHUP")
               if hasattr(signal, name)]
    previous_handlers = {number: signal.getsignal(number) for number in numbers}
    cleanup_events = []
    original_operation = getattr(engine, operation)

    def signal_during_payload(*args, **kwargs):
        monkeypatch.setattr(kwargs["progress"], "chunk", lambda _count: os.kill(os.getpid(), signal.SIGINT))
        return original_operation(*args, **kwargs)

    monkeypatch.setattr(engine, operation, signal_during_payload)
    if operation == "create":
        original_unlink = Path.unlink

        def signal_during_unlink(path, *args, **kwargs):
            if path.parent == tmp_path and path.name.startswith(f".{archive.name}."):
                assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
                os.kill(os.getpid(), signal.SIGINT)
                cleanup_events.append(path.name)
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", signal_during_unlink)
        action = lambda display: engine.create(archive, [source], progress=display)
    else:
        original_abort = core.SafeOutputFile.abort

        def signal_during_abort(output):
            assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
            os.kill(os.getpid(), signal.SIGINT)
            cleanup_events.append(output.entry.name)
            return original_abort(output)

        monkeypatch.setattr(core.SafeOutputFile, "abort", signal_during_abort)
        action = lambda display: engine.extract(archive, destination, progress=display)

    assert snug._menu_run_operation(operation, action, archive=archive) is None
    assert cleanup_events
    assert {number: signal.getsignal(number) for number in numbers} == previous_handlers
    assert "cancelled" in capsys.readouterr().out.lower()
    assert not list(tmp_path.rglob("*.part"))
    assert not list(tmp_path.rglob(".snug-part-*"))
    if operation == "create":
        assert not archive.exists()
    else:
        assert list(destination.iterdir()) == []


def test_operation_terminal_eof_is_not_reported_as_cancelled(
        menu_environment, capsys):
    eof = snug._TerminalEOF()

    def disconnected(_display):
        raise eof

    with pytest.raises(snug._TerminalEOF) as error:
        snug._menu_run_operation("test", disconnected)
    assert error.value is eof
    assert "cancelled" not in capsys.readouterr().out.lower()


@pytest.mark.parametrize("state", ["unreadable", "removed"])
def test_create_cancel_does_not_claim_preservation_when_output_cannot_be_verified(
        monkeypatch, tmp_path, menu_environment, capsys, state):
    archive = tmp_path / "output.zip"
    archive.write_bytes(b"previous archive")

    def cancelled(_display):
        if state == "removed":
            archive.unlink()
        else:
            original = Path.lstat

            def inaccessible(path):
                if path == archive:
                    raise PermissionError("fixture inaccessible")
                return original(path)

            monkeypatch.setattr(Path, "lstat", inaccessible)
        raise KeyboardInterrupt

    assert snug._menu_run_operation("create", cancelled, archive=archive) is None
    assert CREATE_UNKNOWN in capsys.readouterr().out


def test_unreadable_initial_archive_state_cannot_prove_no_publication(
        monkeypatch, tmp_path, menu_environment, capsys):
    archive = tmp_path / "output.zip"
    archive.write_bytes(b"existing output")
    original = Path.lstat

    def inaccessible(path):
        if path == archive:
            raise PermissionError("fixture initial state inaccessible")
        return original(path)

    monkeypatch.setattr(Path, "lstat", inaccessible)

    def cancelled(_display):
        monkeypatch.setattr(Path, "lstat", original)
        raise KeyboardInterrupt

    assert snug._menu_run_operation("create", cancelled, archive=archive) is None
    message = capsys.readouterr().out
    assert CREATE_UNKNOWN in message
    assert CREATE_UNCHANGED not in message
    assert archive.read_bytes() == b"existing output"


def test_real_extraction_cleanup_failure_is_reported_without_exposing_password(
        monkeypatch, tmp_path, menu_environment, capsys):
    archive = zip_inputs(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()
    existing = destination / "first.txt"
    existing.write_bytes(b"existing file must remain")
    password = "fixture-private-password"
    engine = native_engine()
    stop_engine_at(monkeypatch, engine, "extract", "chunk")
    original_unlink = Path.unlink

    def denied_staging_cleanup(path, *args, **kwargs):
        if path.parent == destination and path.name.startswith(".snug-part-"):
            raise PermissionError(f"fixture staging cleanup denied: {password}")
        return original_unlink(path, *args, **kwargs)

    try:
        with monkeypatch.context() as temporary_patch:
            temporary_patch.setattr(Path, "unlink", denied_staging_cleanup)
            report = snug._menu_run_operation(
                "extract", lambda display: engine.extract(archive, destination, progress=display),
                password=password, archive=archive,
            )
        assert report is None
        message = capsys.readouterr().out
        assert "Extraction cancelled." in message
        assert EXTRACT_CANCELLED in message
        assert password not in message
        if hasattr(BaseException, "add_note"):
            assert "warning: cannot clean extraction staging for 'first.txt'" in message
            assert "fixture staging cleanup denied: [redacted]" in message
            assert message.index("Extraction cancelled.") < message.index("warning:")
        else:
            # Python 3.10 has no exception-note API; cancellation still works.
            assert "warning:" not in message
        assert existing.read_bytes() == b"existing file must remain"
        assert list(destination.glob(".snug-part-*"))
    finally:
        for staging in destination.glob(".snug-part-*"):
            staging.unlink()


@pytest.mark.parametrize("has_notes", [False, True])
def test_cancel_details_support_exception_notes_and_their_absence(
        menu_environment, capsys, has_notes):
    password = "fixture-private-password"
    failure = KeyboardInterrupt()
    if has_notes:
        # Assigning this attribute also exercises the fallback on Python 3.10.
        failure.__notes__ = [f"could not remove temporary output: {password}\x1b[31m"]

    def cancelled(_display):
        raise failure

    assert snug._menu_run_operation("test", cancelled, password=password) is None
    message = capsys.readouterr().out
    assert TEST_CANCELLED in message
    assert password not in message
    assert "\x1b" not in message
    if has_notes:
        assert "warning: could not remove temporary output: [redacted]" in message
        assert message.index(TEST_CANCELLED) < message.index("warning:")
    else:
        assert "warning:" not in message


def test_cli_keeps_exit_130_on_operation_interrupt(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(snug, "_start_update_check", lambda: None)
    monkeypatch.setattr(snug.ArchiveEngine, "test", lambda *_args, **_kwargs: interrupt())
    assert snug.main(["test", str(tmp_path / "input.zip")]) == 130
    assert "interrupted" in capsys.readouterr().err
