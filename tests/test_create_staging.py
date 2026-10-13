"""Archive setup cancellation cleans only staging files owned by this run."""

import errno
import os
from pathlib import Path
import signal
import threading
from types import SimpleNamespace
import zipfile

import pytest

import snug_core as core
import snug_ext as ext


def inputs(tmp_path, existing=False):
    source = tmp_path / "source.txt"
    source.write_bytes(b"complete source payload")
    archive = tmp_path / "output.zip"
    previous = b"previous archive bytes"
    if existing:
        archive.write_bytes(previous)
    return source, archive, previous


def record_staging(monkeypatch):
    create = core.tempfile.mkstemp
    created = []

    def record(*args, **kwargs):
        fd, name = create(*args, **kwargs)
        created.append((fd, Path(name)))
        return fd, name

    monkeypatch.setattr(core.tempfile, "mkstemp", record)
    return created


def close_recorded(created, close=os.close):
    for fd, _path in created:
        try:
            close(fd)
        except OSError:
            pass


def assert_output_unchanged(archive, previous, existing):
    if existing:
        assert archive.read_bytes() == previous
    else:
        assert not archive.exists()


@pytest.mark.parametrize("existing", [False, True])
def test_sigint_immediately_after_exclusive_creation_cleans_staging(
        monkeypatch, tmp_path, existing):
    source, archive, previous = inputs(tmp_path, existing)
    create = core.tempfile.mkstemp
    created = []
    interruption = KeyboardInterrupt()
    handlers = {number: signal.getsignal(number) for number in (
        signal.SIGINT, signal.SIGTERM,
        *([signal.SIGHUP] if hasattr(signal, "SIGHUP") else []),
    )}

    def interrupt(_number, _frame):
        raise interruption

    def signal_after_creation(*args, **kwargs):
        fd, name = create(*args, **kwargs)
        created.append((fd, Path(name)))
        os.kill(os.getpid(), signal.SIGINT)
        return fd, name

    monkeypatch.setattr(core.tempfile, "mkstemp", signal_after_creation)
    signal.signal(signal.SIGINT, interrupt)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            core.ArchiveEngine([core.NativeBackend()]).create(archive, [source])
        assert caught.value is interruption
        assert signal.getsignal(signal.SIGINT) is interrupt
        assert all(signal.getsignal(number) is handler for number, handler in handlers.items()
                   if number != signal.SIGINT)
        assert created
        assert not list(tmp_path.glob(f".{archive.name}.*.part"))
        assert_output_unchanged(archive, previous, existing)
        with pytest.raises(OSError) as closed:
            os.fstat(created[0][0])
        assert closed.value.errno == errno.EBADF
    finally:
        signal.signal(signal.SIGINT, handlers[signal.SIGINT])
        close_recorded(created)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", [
    KeyboardInterrupt(), SystemExit(7), OSError(errno.EIO, "setup close failure"),
    ValueError("setup close failure"),
], ids=["ctrl-c", "system-exit", "io-error", "value-error"])
def test_baseexception_after_staging_close_cleans_only_this_run(
        monkeypatch, tmp_path, existing, failure):
    source, archive, previous = inputs(tmp_path, existing)
    created = record_staging(monkeypatch)
    close = os.close
    interrupted = []

    def close_then_fail(fd):
        close(fd)
        if created and fd == created[0][0] and not interrupted:
            interrupted.append(True)
            raise failure

    monkeypatch.setattr(core.os, "close", close_then_fail)
    expected = core.ArchiveError if isinstance(failure, OSError) else type(failure)
    try:
        with pytest.raises(expected) as caught:
            core.ArchiveEngine([core.NativeBackend()]).create(archive, [source])
        assert caught.value.__cause__ is failure if isinstance(failure, OSError) else caught.value is failure
        assert not list(tmp_path.glob(f".{archive.name}.*.part"))
        assert_output_unchanged(archive, previous, existing)
    finally:
        close_recorded(created, close)


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), SystemExit(7)], ids=["ctrl-c", "system-exit"])
def test_setup_cleanup_preserves_a_replacement_staging_inode(monkeypatch, tmp_path, failure):
    source, archive, _previous = inputs(tmp_path)
    created = record_staging(monkeypatch)
    replacement = tmp_path / "replacement.txt"
    replacement.write_bytes(b"unrelated replacement must survive")
    close = os.close
    interrupted = []

    def replace_then_fail(fd):
        close(fd)
        if created and fd == created[0][0] and not interrupted:
            interrupted.append(True)
            os.replace(replacement, created[0][1])
            raise failure

    monkeypatch.setattr(core.os, "close", replace_then_fail)
    try:
        with pytest.raises(type(failure)):
            core.ArchiveEngine([core.NativeBackend()]).create(archive, [source])
        assert created[0][1].read_bytes() == b"unrelated replacement must survive"
        assert not archive.exists()
    finally:
        close_recorded(created, close)


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), core.ArchiveError("backend failure")],
                         ids=["ctrl-c", "archive-error"])
def test_backend_cleanup_preserves_a_replacement_staging_inode(monkeypatch, tmp_path, failure):
    source, archive, _previous = inputs(tmp_path)
    replacement = tmp_path / "replacement.txt"
    replacement.write_bytes(b"unrelated replacement must survive")
    backend = core.NativeBackend()
    staged = []

    def replace_then_fail(path, *_args, **_kwargs):
        staged.append(path)
        os.replace(replacement, path)
        raise failure

    monkeypatch.setattr(backend, "create", replace_then_fail)
    with pytest.raises(type(failure)):
        core.ArchiveEngine([backend]).create(archive, [source])
    assert staged[0].read_bytes() == b"unrelated replacement must survive"
    assert not archive.exists()


def test_cancelled_setup_preserves_a_preexisting_name_collision(monkeypatch, tmp_path):
    source, archive, _previous = inputs(tmp_path)
    collision = tmp_path / f".{archive.name}.collision.part"
    collision.write_bytes(b"preexisting unrelated file")
    monkeypatch.setattr(core.tempfile, "_get_candidate_names", lambda: iter(["collision", "created"]))
    create = core.tempfile.mkstemp
    created = []

    def signal_after_creation(*args, **kwargs):
        fd, name = create(*args, **kwargs)
        created.append((fd, Path(name)))
        os.kill(os.getpid(), signal.SIGINT)
        return fd, name

    monkeypatch.setattr(core.tempfile, "mkstemp", signal_after_creation)
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            core.ArchiveEngine([core.NativeBackend()]).create(archive, [source])
        assert collision.read_bytes() == b"preexisting unrelated file"
        assert list(tmp_path.glob(f".{archive.name}.*.part")) == [collision]
        assert not archive.exists()
    finally:
        signal.signal(signal.SIGINT, previous)
        close_recorded(created)


def test_ignored_sigint_stays_ignored_during_staging_setup(monkeypatch, tmp_path):
    source, archive, _previous = inputs(tmp_path)
    create = core.tempfile.mkstemp

    def signal_after_creation(*args, **kwargs):
        result = create(*args, **kwargs)
        os.kill(os.getpid(), signal.SIGINT)
        return result

    monkeypatch.setattr(core.tempfile, "mkstemp", signal_after_creation)
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        core.ArchiveEngine([core.NativeBackend()]).create(archive, [source])
        assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
        with zipfile.ZipFile(archive) as completed:
            assert completed.read(source.name) == source.read_bytes()
        assert not list(tmp_path.glob(f".{archive.name}.*.part"))
    finally:
        signal.signal(signal.SIGINT, previous)


def test_worker_thread_setup_cancellation_needs_no_signal_handler_change(monkeypatch, tmp_path):
    source, archive, _previous = inputs(tmp_path)
    created = record_staging(monkeypatch)
    close = os.close
    interrupted = []
    caught = []
    previous = signal.getsignal(signal.SIGINT)

    def close_then_fail(fd):
        close(fd)
        if created and fd == created[0][0] and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt

    def create_in_worker():
        try:
            core.ArchiveEngine([core.NativeBackend()]).create(archive, [source])
        except BaseException as error:
            caught.append(error)

    monkeypatch.setattr(core.os, "close", close_then_fail)
    try:
        worker = threading.Thread(target=create_in_worker)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert len(caught) == 1 and isinstance(caught[0], KeyboardInterrupt)
        assert signal.getsignal(signal.SIGINT) is previous
        assert not list(tmp_path.glob(f".{archive.name}.*.part"))
        assert not archive.exists()
    finally:
        close_recorded(created, close)


@pytest.mark.parametrize("factory", [False, True], ids=["native-zip", "7z-factory"])
@pytest.mark.parametrize("existing", [False, True])
def test_extraction_sigint_after_exclusive_open_registers_staging_before_cancelling(
        monkeypatch, tmp_path, factory, existing):
    archive = tmp_path / "input.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("first.txt", b"new first payload")
        handle.writestr("second.txt", b"new second payload")
    destination = tmp_path / "destination"
    destination.mkdir()
    first = destination / "first.txt"
    if existing:
        first.write_bytes(b"previous first payload")
    backend = core.NativeBackend()
    if factory:
        # Exercise the actual 7z writer factory and SafeOutputFile without
        # requiring an optional decompression library for this acquisition test.
        library = SimpleNamespace(io=SimpleNamespace(WriterFactory=object))

        def extract_with_factory(_path, ctx, password=None):
            writers = ext.SevenZipBackend._factory(library, ctx.inspection.entries, ctx)
            try:
                writers.create("first.txt")
            finally:
                writers.close(abort=True)

        monkeypatch.setattr(backend, "extract", extract_with_factory)
    opened = []
    original_open = os.open
    handlers = {number: signal.getsignal(number) for number in (
        signal.SIGINT, signal.SIGTERM,
        *([signal.SIGHUP] if hasattr(signal, "SIGHUP") else []),
    )}
    interruption = KeyboardInterrupt()

    def interrupt(_number, _frame):
        raise interruption

    def signal_after_open(path, *args, **kwargs):
        fd = original_open(path, *args, **kwargs)
        if Path(path).name.startswith(".snug-part-"):
            opened.append((fd, Path(path)))
            os.kill(os.getpid(), signal.SIGINT)
        return fd

    monkeypatch.setattr(core.os, "open", signal_after_open)
    signal.signal(signal.SIGINT, interrupt)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            core.ArchiveEngine([backend]).extract(archive, destination)
        assert caught.value is interruption
        assert signal.getsignal(signal.SIGINT) is interrupt
        assert all(signal.getsignal(number) is handler for number, handler in handlers.items()
                   if number != signal.SIGINT)
        assert opened
        assert not list(destination.glob(".snug-part-*"))
        if existing:
            assert first.read_bytes() == b"previous first payload"
        else:
            assert not first.exists()
        assert not (destination / "second.txt").exists()
        with pytest.raises(OSError) as closed:
            os.fstat(opened[0][0])
        assert closed.value.errno == errno.EBADF
    finally:
        signal.signal(signal.SIGINT, handlers[signal.SIGINT])
        close_recorded(opened)
