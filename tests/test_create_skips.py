"""Accidental create omissions must be visible and must not replace good output."""

import builtins
from contextlib import nullcontext
import errno
import os
from pathlib import Path
import queue
import subprocess
import sys
import tarfile
import threading

import pytest

import snug
import snug_core as core


NATIVE = ["zip", "tar", "tar.gz", "tar.bz2", "tar.xz"]
SCRIPT = Path(__file__).resolve().parents[1] / "snug.py"


@pytest.fixture(autouse=True)
def no_automatic_check(monkeypatch):
    monkeypatch.setattr(snug, "_start_update_check", lambda: None)


def deny_reads(monkeypatch, paths):
    original = builtins.open
    denied = {Path(path) for path in paths}

    def open_source(file, mode="r", *args, **kwargs):
        if not isinstance(file, int) and Path(file) in denied and "r" in mode:
            raise PermissionError(errno.EACCES, "Permission denied", str(file))
        return original(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", open_source)


def sources(tmp_path):
    readable = tmp_path / "readable.txt"
    unreadable = tmp_path / "unreadable.txt"
    readable.write_bytes(b"readable payload")
    unreadable.write_bytes(b"unreadable payload")
    return readable, unreadable


def assert_no_staging(archive):
    assert not list(archive.parent.glob(f".{archive.name}.*.part"))


def assert_skip_warning(stderr, source):
    assert str(source) in stderr
    assert "permission denied" in stderr.lower()
    assert "warning:" in stderr
    assert "Traceback" not in stderr


@pytest.mark.parametrize("suffix", ["zip", "tar", "tar.gz"])
@pytest.mark.parametrize("quiet", [False, True])
def test_cli_partial_create_warns_even_when_quiet(tmp_path, monkeypatch, capsys, suffix, quiet):
    readable, unreadable = sources(tmp_path)
    archive = tmp_path / f"partial.{suffix}"
    deny_reads(monkeypatch, [unreadable])
    code = snug.main(["create", str(archive), str(readable), str(unreadable), *( ["-q"] if quiet else [])])
    captured = capsys.readouterr()
    assert code == 5
    assert_skip_warning(captured.err, unreadable)
    assert "incomplete" in captured.err.lower()
    assert "1 included" in captured.err and "1 skipped" in captured.err
    assert (captured.out == "") if quiet else "Created " in captured.out
    engine = core.ArchiveEngine([core.NativeBackend()])
    assert [entry.name for entry in engine.list_entries(archive)] == [readable.name]
    assert engine.test(archive).files == 1
    assert_no_staging(archive)


@pytest.mark.parametrize("suffix", ["zip", "tar", "tar.gz"])
@pytest.mark.parametrize("existing", [False, True])
def test_cli_all_accidentally_skipped_publishes_nothing(tmp_path, monkeypatch, capsys, suffix, existing):
    _, unreadable = sources(tmp_path)
    archive = tmp_path / f"all-skipped.{suffix}"
    previous = b"previous archive bytes"
    if existing:
        archive.write_bytes(previous)
    deny_reads(monkeypatch, [unreadable])
    code = snug.main(["create", str(archive), str(unreadable), "--force", "-q"])
    captured = capsys.readouterr()
    assert code == 2
    assert_skip_warning(captured.err, unreadable)
    assert "error:" in captured.err
    assert captured.out == ""
    if existing:
        assert archive.read_bytes() == previous
    else:
        assert not archive.exists()
    assert_no_staging(archive)


@pytest.mark.parametrize("suffix", ["zip", "tar", "tar.gz"])
@pytest.mark.parametrize("quiet", [False, True])
def test_cli_forced_partial_preserves_existing_archive(tmp_path, monkeypatch, capsys, suffix, quiet):
    readable, unreadable = sources(tmp_path)
    archive = tmp_path / f"existing.{suffix}"
    engine = core.ArchiveEngine([core.NativeBackend()])
    engine.create(archive, [unreadable])
    previous = archive.read_bytes()
    deny_reads(monkeypatch, [unreadable])
    code = snug.main(["create", str(archive), str(readable), str(unreadable), "--force", *( ["-q"] if quiet else [])])
    captured = capsys.readouterr()
    assert code == 2
    assert archive.read_bytes() == previous
    assert_skip_warning(captured.err, unreadable)
    assert "error:" in captured.err and "preserv" in captured.err.lower()
    assert str(archive) in captured.err
    assert "Created " not in captured.out
    assert_no_staging(archive)


@pytest.mark.parametrize("suffix", ["zip", "tar", "tar.gz"])
def test_partial_publication_preserves_archive_appearing_at_commit(tmp_path, monkeypatch, suffix):
    readable, unreadable = sources(tmp_path)
    archive = tmp_path / f"racing-output.{suffix}"
    other = tmp_path / f"original.{suffix}"
    engine = core.ArchiveEngine([core.NativeBackend()])
    engine.create(other, [unreadable])
    previous = other.read_bytes()
    replace = os.replace
    link = os.link

    def racing_replace(source, target, *args, **kwargs):
        if Path(target) == archive:
            archive.write_bytes(previous)
        return replace(source, target, *args, **kwargs)

    def racing_link(source, target, *args, **kwargs):
        if Path(target) == archive:
            archive.write_bytes(previous)
        return link(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "replace", racing_replace)
    monkeypatch.setattr(os, "link", racing_link)
    deny_reads(monkeypatch, [unreadable])
    with pytest.raises(core.CreateError) as error:
        engine.create(archive, [readable, unreadable])
    assert error.value.preserved_existing is True
    assert error.value.report.included_inputs == 1
    assert error.value.report.accidental_skips == 1
    assert archive.read_bytes() == previous
    assert engine.test(archive).files == 1
    assert_no_staging(archive)


@pytest.mark.parametrize("suffix", ["zip", "tar", "tar.gz"])
def test_published_partial_stays_incomplete_when_staging_cleanup_fails(
        tmp_path, monkeypatch, capsys, suffix):
    readable, unreadable = sources(tmp_path)
    archive = tmp_path / f"cleanup-failure.{suffix}"
    unlink = Path.unlink
    owned_staging = []

    def fail_staging_cleanup(path, *args, **kwargs):
        if (path.parent == archive.parent and path.name.startswith(f".{archive.name}.")
                and path.name.endswith(".part")):
            owned_staging.append(path)
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(Path, "unlink", fail_staging_cleanup)
        deny_reads(context, [unreadable])
        code = snug.main(["create", str(archive), str(readable), str(unreadable), "-q"])
        captured = capsys.readouterr()
    try:
        assert code == 5
        assert captured.out == ""
        assert owned_staging
        assert str(owned_staging[0]) in captured.err
        assert "warning:" in captured.err and "permission denied" in captured.err.lower()
        assert "cleanup" in captured.err.lower() or "temporary" in captured.err.lower()
        assert "incomplete" in captured.err.lower()
        assert "1 included" in captured.err and "1 skipped" in captured.err
        assert "error:" not in captured.err
        assert captured.err.rstrip().splitlines()[-1].startswith("warning: archive is incomplete:")
        engine = core.ArchiveEngine([core.NativeBackend()])
        assert engine.test(archive).files == 1
        assert [entry.name for entry in engine.list_entries(archive)] == [readable.name]
    finally:
        for path in set(owned_staging):
            path.unlink(missing_ok=True)
    assert_no_staging(archive)


@pytest.mark.parametrize("suffix", NATIVE)
def test_native_partial_report_counts_inputs_and_records_cause(tmp_path, monkeypatch, suffix):
    readable, unreadable = sources(tmp_path)
    archive = tmp_path / f"partial.{suffix}"
    deny_reads(monkeypatch, [unreadable])
    engine = core.ArchiveEngine([core.NativeBackend()])
    report = engine.create(archive, [readable, unreadable])
    assert report.included_inputs == 1
    assert report.accidental_skips == 1
    assert len(report.skip_errors) == 1
    assert report.skip_errors[0].path == unreadable
    assert "permission denied" in report.skip_errors[0].reason.lower()
    assert [entry.name for entry in engine.list_entries(archive)] == [readable.name]
    destination = tmp_path / "extracted"
    engine.extract(archive, destination)
    assert (destination / readable.name).read_bytes() == readable.read_bytes()
    assert not (destination / unreadable.name).exists()
    assert_no_staging(archive)


@pytest.mark.parametrize("suffix", NATIVE)
def test_file_vanishing_during_walk_is_accidental(tmp_path, monkeypatch, suffix):
    readable, vanished = sources(tmp_path)
    archive = tmp_path / f"vanished.{suffix}"
    walk = core._walk

    def disappearing_walk(path, *args, **kwargs):
        for source, name in walk(path, *args, **kwargs):
            if source == vanished:
                source.unlink()
            yield source, name

    monkeypatch.setattr(core, "_walk", disappearing_walk)
    engine = core.ArchiveEngine([core.NativeBackend()])
    report = engine.create(archive, [readable, vanished])
    assert report.included_inputs == 1 and report.accidental_skips == 1
    assert report.skip_errors[0].path == vanished
    assert "no such file" in report.skip_errors[0].reason.lower()
    assert [entry.name for entry in engine.list_entries(archive)] == [readable.name]
    assert_no_staging(archive)


@pytest.mark.parametrize("suffix", ["tar", "tar.gz", "tar.bz2", "tar.xz"])
def test_skipped_tar_source_does_not_leave_a_dangling_hardlink(tmp_path, monkeypatch, suffix):
    inaccessible = tmp_path / "inaccessible.txt"
    accessible_alias = tmp_path / "accessible-alias.txt"
    inaccessible.write_bytes(b"payload from the readable hardlink")
    os.link(inaccessible, accessible_alias)
    deny_reads(monkeypatch, [inaccessible])
    engine = core.ArchiveEngine([core.NativeBackend()])
    archive = tmp_path / f"hardlink-after-skip.{suffix}"
    report = engine.create(archive, [inaccessible, accessible_alias])
    assert report.included_inputs == 1 and report.accidental_skips == 1
    assert engine.test(archive).files == 1
    destination = tmp_path / "extracted"
    engine.extract(archive, destination)
    assert (destination / accessible_alias.name).read_bytes() == accessible_alias.read_bytes()
    assert not (destination / inaccessible.name).exists()


@pytest.mark.parametrize("suffix", NATIVE)
@pytest.mark.parametrize("existing", [False, True])
def test_mid_payload_read_failure_aborts_without_publishing(tmp_path, monkeypatch, suffix, existing):
    source = tmp_path / "large.txt"
    source.write_bytes(b"x" * (core.CHUNK_SIZE + 1))
    archive = tmp_path / f"read-failure.{suffix}"
    previous = b"previous archive bytes"
    if existing:
        archive.write_bytes(previous)
    original = builtins.open

    class FailingReader:
        def __init__(self, stream):
            self.stream = stream
            self.reads = 0

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def read(self, size=-1):
            self.reads += 1
            if self.reads > 1:
                raise PermissionError(errno.EACCES, "read permission lost", str(source))
            return self.stream.read(size)

        def __getattr__(self, name):
            return getattr(self.stream, name)

    def fail_during_read(file, mode="r", *args, **kwargs):
        stream = original(file, mode, *args, **kwargs)
        if not isinstance(file, int) and Path(file) == source and "r" in mode:
            return FailingReader(stream)
        return stream

    monkeypatch.setattr(builtins, "open", fail_during_read)
    engine = core.ArchiveEngine([core.NativeBackend()])
    with pytest.raises(core.ArchiveError):
        engine.create(archive, [source])
    if existing:
        assert archive.read_bytes() == previous
    else:
        assert not archive.exists()
    assert_no_staging(archive)


@pytest.mark.parametrize("quiet", [False, True])
def test_cli_warns_for_first_twenty_omissions_then_counts_remainder(tmp_path, monkeypatch, capsys, quiet):
    readable, _ = sources(tmp_path)
    omitted = [tmp_path / f"omitted-{i:02}.txt" for i in range(25)]
    for path in omitted:
        path.write_bytes(b"omitted")
    deny_reads(monkeypatch, omitted)
    archive = tmp_path / "many-skips.zip"
    code = snug.main(["create", str(archive), str(readable), *map(str, omitted), *( ["-q"] if quiet else [])])
    captured = capsys.readouterr()
    assert code == 5
    for path in omitted[:20]:
        assert str(path) in captured.err
    for path in omitted[20:]:
        assert str(path) not in captured.err
    assert "5" in captured.err and any(word in captured.err.lower() for word in ["more", "additional"])
    assert "25 skipped" in captured.err
    assert "1 included" in captured.err
    assert captured.out == "" if quiet else "Created " in captured.out


@pytest.mark.parametrize("suffix", ["zip", "tar", "tar.gz"])
@pytest.mark.parametrize("quiet", [False, True])
def test_cli_real_unreadable_input_is_visible(tmp_path, suffix, quiet):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can read chmod 000 inputs")
    if os.name == "nt":
        pytest.skip("chmod does not remove Windows read access")
    readable, unreadable = sources(tmp_path)
    archive = tmp_path / f"chmod-partial.{suffix}"
    previous_mode = unreadable.stat().st_mode & 0o777
    unreadable.chmod(0)
    try:
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "create", str(archive), str(readable), str(unreadable), *( ["-q"] if quiet else [])],
            env=dict(os.environ, SNUG_NO_UPDATE_CHECK="1"), text=True,
            capture_output=True, timeout=30,
        )
    finally:
        unreadable.chmod(previous_mode)
    assert result.returncode == 5, result.stdout + result.stderr
    assert_skip_warning(result.stderr, unreadable)
    assert "incomplete" in result.stderr.lower()
    assert result.stdout == "" if quiet else "Created " in result.stdout
    engine = core.ArchiveEngine([core.NativeBackend()])
    assert engine.test(archive).files == 1
    assert [entry.name for entry in engine.list_entries(archive)] == [readable.name]


@pytest.mark.parametrize("suffix", ["tar", "tar.gz", "tar.bz2", "tar.xz"])
def test_tar_special_record_counts_as_included_input(tmp_path, monkeypatch, suffix):
    if not hasattr(os, "mkfifo"):
        pytest.skip("platform cannot create FIFO inputs")
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    _, unreadable = sources(tmp_path)
    deny_reads(monkeypatch, [unreadable])
    engine = core.ArchiveEngine([core.NativeBackend()])
    archive = tmp_path / f"special.{suffix}"
    report = engine.create(archive, [pipe, unreadable])
    assert report.entries == 0
    assert report.included_inputs == 1
    assert report.accidental_skips == 1
    with tarfile.open(archive, "r:*") as output:
        assert output.getnames() == ["pipe"]
        assert output.getmembers()[0].isfifo()
    assert engine.test(archive).entries == 1


@pytest.mark.parametrize("same_file", ["path", "symlink", "hardlink"])
def test_same_real_file_name_collision_remains_intentional(tmp_path, same_file):
    first = tmp_path / "first/same.txt"
    first.parent.mkdir()
    first.write_bytes(b"original")
    second = first
    if same_file != "path":
        second = tmp_path / "second/same.txt"
        second.parent.mkdir()
        if same_file == "symlink":
            second.symlink_to(first)
        else:
            os.link(first, second)
    engine = core.ArchiveEngine([core.NativeBackend()])
    archive = tmp_path / "same-file.zip"
    report = engine.create(archive, [first, second])
    assert report.included_inputs == 1 and report.accidental_skips == 0
    assert report.skip_errors == []
    assert [entry.name for entry in engine.list_entries(archive)] == [first.name]


@pytest.mark.parametrize("suffix", ["zip", "tar", "tar.gz"])
@pytest.mark.parametrize("existing", [False, True])
def test_different_file_name_collision_is_accidental(tmp_path, capsys, suffix, existing):
    first = tmp_path / "first/same.txt"
    second = tmp_path / "second/same.txt"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    archive = tmp_path / f"collision.{suffix}"
    previous = b"keep previous"
    if existing:
        archive.write_bytes(previous)
    code = snug.main(["create", str(archive), str(first), str(second), "--force", "-q"])
    captured = capsys.readouterr()
    assert code == (2 if existing else 5)
    assert "warning:" in captured.err and str(second) in captured.err
    assert first.name in captured.err
    assert any(word in captured.err.lower() for word in ["collision", "same archive name", "already", "duplicate", "also belongs"])
    if existing:
        assert archive.read_bytes() == previous
    else:
        engine = core.ArchiveEngine([core.NativeBackend()])
        output = tmp_path / "extracted"
        engine.extract(archive, output)
        assert (output / first.name).read_bytes() == b"first"
    assert_no_staging(archive)


@pytest.mark.parametrize("suffix", NATIVE)
def test_intentional_symlink_skip_keeps_success(tmp_path, capsys, suffix):
    readable, _ = sources(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(readable.name)
    archive = tmp_path / f"intentional.{suffix}"
    assert snug.main(["create", str(archive), str(readable), str(link), "--symlinks", "skip", "-q"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    engine = core.ArchiveEngine([core.NativeBackend()])
    assert [entry.name for entry in engine.list_entries(archive)] == [readable.name]


def test_output_itself_empty_root_and_follow_cycle_remain_intentional(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    source = tree / "input.txt"
    source.write_bytes(b"input")
    (tree / "cycle").symlink_to(".", target_is_directory=True)
    archive = tree / "archive.zip"
    archive.write_bytes(b"existing")
    engine = core.ArchiveEngine([core.NativeBackend()])
    report = engine.create(archive, [tree], root=tree, symlinks="follow")
    assert report.included_inputs == 1 and report.accidental_skips == 0
    assert report.skip_errors == []
    assert [entry.name for entry in engine.list_entries(archive)] == [source.name]


@pytest.mark.parametrize("case", ["partial", "existing", "all-skipped"])
def test_tui_create_uses_shared_engine_and_shows_omissions(tmp_path, monkeypatch, capsys, case):
    readable, unreadable = sources(tmp_path)
    archive = tmp_path / "tui.zip"
    previous = b"existing archive"
    if case == "existing":
        archive.write_bytes(previous)
    chosen = [unreadable] if case == "all-skipped" else [readable, unreadable]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(snug, "_select_sources_arrow", lambda cwd: chosen)

    def configure(options):
        options.archive_path = archive
        options.fmt = core.ArchiveFormat.ZIP
        return True

    monkeypatch.setattr(snug, "_configure_create", configure)
    monkeypatch.setattr(snug, "_menu_confirm_overwrite", lambda path: True)
    monkeypatch.setattr(snug, "_clear_screen", lambda: None)
    monkeypatch.setattr(snug, "_wait_for_enter", lambda: None)
    monkeypatch.setattr(snug, "_menu_result_output", nullcontext)
    monkeypatch.setattr(snug, "ProgressDisplay", lambda *args, **kwargs: core.NullProgress())
    deny_reads(monkeypatch, [unreadable])
    engine = core.ArchiveEngine([core.NativeBackend()])
    assert snug._run_menu_handler(engine, snug._menu_create) is None
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert str(unreadable) in output and "permission denied" in output.lower()
    if case == "partial":
        assert "incomplete" in output.lower()
        assert "1 included" in output and "1 skipped" in output
        assert engine.test(archive).files == 1
    elif case == "existing":
        assert "error:" in output and "preserv" in output.lower()
        assert archive.read_bytes() == previous
    else:
        assert "error:" in output and not archive.exists()
    assert_no_staging(archive)


@pytest.fixture(params=["cpio", "ar", "7z"])
def extended_writer(request):
    import snug_ext

    if request.param == "7z":
        request.getfixturevalue("py7zr_backend")
        backend = snug_ext.SevenZipBackend()
    else:
        request.getfixturevalue("libarchive_backend")
        backend = snug_ext.LibarchiveBackend()
    return core.ArchiveEngine([backend]), request.param


@pytest.mark.parametrize("case", ["partial", "existing-partial", "all-skipped", "existing-all"])
@pytest.mark.parametrize("error_number", [errno.ENOENT, errno.EACCES, errno.EIO])
def test_extended_initial_metadata_failure_obeys_publication_policy(
        tmp_path, monkeypatch, extended_writer, case, error_number):
    engine, suffix = extended_writer
    readable, inaccessible = sources(tmp_path)
    archive = tmp_path / f"stat-failure.{suffix}"
    previous = b"previous archive bytes"
    existing = case.startswith("existing")
    all_skipped = case in {"all-skipped", "existing-all"}
    if existing:
        archive.write_bytes(previous)
    original = Path.lstat

    def failing_lstat(path, *args, **kwargs):
        if path == inaccessible:
            raise OSError(error_number, os.strerror(error_number), str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", failing_lstat)
    items = [(inaccessible, inaccessible.name)] if all_skipped else [
        (readable, readable.name), (inaccessible, inaccessible.name)]
    precollected = (items, sum(path.stat().st_size for path, _ in items))
    if case == "partial":
        report = engine.create(archive, [path for path, _ in items], precollected=precollected)
        assert report.included_inputs == 1 and report.accidental_skips == 1
        assert report.skip_errors[0].path == inaccessible
        assert os.strerror(error_number) in report.skip_errors[0].reason
        assert engine.test(archive).files == 1
        assert [entry.name for entry in engine.list_entries(archive)] == [readable.name]
        output = tmp_path / "output"
        engine.extract(archive, output)
        assert (output / readable.name).read_bytes() == readable.read_bytes()
    else:
        with pytest.raises(core.CreateError) as error:
            engine.create(archive, [path for path, _ in items], precollected=precollected)
        report = error.value.report
        assert report.included_inputs == (0 if all_skipped else 1)
        assert report.accidental_skips == 1
        assert report.skip_errors[0].path == inaccessible
        if existing:
            assert archive.read_bytes() == previous
        else:
            assert not archive.exists()
    assert_no_staging(archive)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("quiet", [False, True])
def test_sevenzip_payload_permission_failure_keeps_existing_abort(
        tmp_path, monkeypatch, capsys, py7zr_backend, existing, quiet):
    readable, unreadable = sources(tmp_path)
    archive = tmp_path / "payload-error.7z"
    previous = b"previous archive bytes"
    if existing:
        archive.write_bytes(previous)
    original = Path.open

    def failing_open(path, mode="r", *args, **kwargs):
        if path == unreadable and "r" in mode:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    code = snug.main(["create", str(archive), str(readable), str(unreadable), "--force", *( ["-q"] if quiet else [])])
    captured = capsys.readouterr()
    assert code == 2
    assert "error:" in captured.err
    assert str(unreadable) in captured.err and "permission denied" in captured.err.lower()
    assert "Created " not in captured.out
    if existing:
        assert archive.read_bytes() == previous
    else:
        assert not archive.exists()
    assert_no_staging(archive)


@pytest.mark.parametrize("suffix", ["cpio", "7z"])
def test_extended_unsupported_source_type_remains_intentional(
        tmp_path, request, suffix):
    if not hasattr(os, "mkfifo"):
        pytest.skip("platform cannot create FIFO inputs")
    request.getfixturevalue("py7zr_backend" if suffix == "7z" else "libarchive_backend")
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    archive = tmp_path / f"unsupported.{suffix}"
    report = core.ArchiveEngine().create(archive, [pipe])
    assert report.included_inputs == 0 and report.accidental_skips == 0
    assert report.skip_errors == [] and report.skipped == [pipe.name]
    assert archive.exists()


def test_precollected_collision_errors_reach_engine_publication_gate(tmp_path):
    readable, _ = sources(tmp_path)
    dropped = tmp_path / "other/readable.txt"
    dropped.parent.mkdir()
    dropped.write_bytes(b"different input")
    archive = tmp_path / "precollected.zip"
    previous = b"keep archive"
    archive.write_bytes(previous)
    skip = core.CreateSkip(dropped, "same archive name as another input")
    with pytest.raises(core.CreateError) as error:
        core.ArchiveEngine([core.NativeBackend()]).create(
            archive, [readable, dropped],
            precollected=([(readable, readable.name)], readable.stat().st_size, [skip]),
        )
    assert error.value.report.included_inputs == 1
    assert error.value.report.accidental_skips == 1
    assert error.value.report.skip_errors == [skip]
    assert archive.read_bytes() == previous
    assert_no_staging(archive)


def colliding_sources(tmp_path):
    left = tmp_path / "left/same.txt"
    right = tmp_path / "right/same.txt"
    left.parent.mkdir()
    right.parent.mkdir()
    left.write_bytes(b"left payload")
    right.write_bytes(b"right payload")
    return left, right


@pytest.mark.parametrize("failure", ["missing-source", "staging-permission"])
@pytest.mark.parametrize("quiet", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_cli_reports_collection_skip_even_when_creation_aborts_before_staging(
        tmp_path, monkeypatch, capsys, failure, quiet, existing):
    left, right = colliding_sources(tmp_path)
    missing = tmp_path / "missing.txt"
    archive = tmp_path / "collection-failure.zip"
    previous = b"previous archive bytes"
    if existing:
        archive.write_bytes(previous)
    selected = [left, right]
    if failure == "missing-source":
        selected.append(missing)
    else:
        def unavailable_staging(*args, **kwargs):
            raise PermissionError(errno.EACCES, "staging permission denied", str(archive.parent))

        monkeypatch.setattr(core.tempfile, "mkstemp", unavailable_staging)
    code = snug.main(["create", str(archive), *map(str, selected), "--force", *( ["-q"] if quiet else [])])
    captured = capsys.readouterr()
    assert code == 2
    assert "warning:" in captured.err and str(right) in captured.err
    assert "same.txt" in captured.err and "also belongs" in captured.err
    assert "error:" in captured.err
    assert captured.err.index("warning:") < captured.err.index("error:")
    assert "Created " not in captured.out
    if quiet:
        assert captured.out == "" and "create:" not in captured.err
    if failure == "missing-source":
        assert str(missing) in captured.err and "no such file" in captured.err.lower()
    else:
        assert "staging permission denied" in captured.err.lower()
    if existing:
        assert archive.read_bytes() == previous
    else:
        assert not archive.exists()
    assert_no_staging(archive)


@pytest.mark.parametrize("existing", [False, True])
def test_tui_reports_prior_skip_when_enumeration_later_fails(
        tmp_path, monkeypatch, capsys, existing):
    left, right = colliding_sources(tmp_path)
    missing = tmp_path / "missing.txt"
    archive = tmp_path / "tui-enumeration-failure.zip"
    previous = b"previous archive bytes"
    if existing:
        archive.write_bytes(previous)
    chosen = [left, right, missing]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(snug, "_select_sources_arrow", lambda cwd: chosen)

    def configure(options):
        options.archive_path = archive
        options.fmt = core.ArchiveFormat.ZIP
        return True

    # Ordinary TUI root-relative names distinguish these folders. Use flat
    # names here to exercise real collision diagnostics through the result path.
    iterator = core._iter_items

    def flat_names(sources, **kwargs):
        kwargs["root"] = None
        yield from iterator(sources, **kwargs)

    monkeypatch.setattr(core, "_iter_items", flat_names)
    monkeypatch.setattr(snug, "_iter_items", flat_names)
    monkeypatch.setattr(snug, "_configure_create", configure)
    monkeypatch.setattr(snug, "_menu_confirm_overwrite", lambda path: True)
    monkeypatch.setattr(snug, "_clear_screen", lambda: None)
    monkeypatch.setattr(snug, "_wait_for_enter", lambda: None)
    monkeypatch.setattr(snug, "_menu_result_output", nullcontext)
    monkeypatch.setattr(snug, "ProgressDisplay", lambda *args, **kwargs: core.NullProgress())
    assert snug._run_menu_handler(core.ArchiveEngine([core.NativeBackend()]), snug._menu_create) is None
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert str(right) in output and "warning:" in captured.err
    assert "also belongs" in captured.err
    assert str(missing) in output and "error:" in output
    assert "Created " not in output
    if existing:
        assert archive.read_bytes() == previous
    else:
        assert not archive.exists()
    assert_no_staging(archive)


def test_enumeration_worker_keeps_prior_skips_in_caller_collector(tmp_path):
    left, right = colliding_sources(tmp_path)
    missing = tmp_path / "missing.txt"
    archive = tmp_path / "not-created.zip"
    messages = queue.Queue()
    result = [None]
    skipped = []
    snug._enumerate_worker(
        [left, right, missing], None, "store", os.path.realpath(archive),
        messages, threading.Event(), result, skip_errors=skipped,
    )
    assert isinstance(result[0], core.ArchiveError)
    assert str(missing) in str(result[0])
    assert len(skipped) == 1 and skipped[0].path == right
    assert "also belongs" in skipped[0].reason
    recorded = []
    while not messages.empty():
        recorded.append(messages.get_nowait())
    assert recorded[-1] == ("error",)
    assert not archive.exists()


def test_non_tty_enumeration_keeps_prior_skips_in_caller_collector(tmp_path):
    left, right = colliding_sources(tmp_path)
    missing = tmp_path / "missing.txt"
    archive = tmp_path / "not-created.zip"
    skipped = []
    with pytest.raises(core.ArchiveError, match="missing.txt"):
        snug._interactive_enumerate(
            [left, right, missing], root=None, symlinks="store",
            archive_real=os.path.realpath(archive), skip_errors=skipped,
        )
    assert len(skipped) == 1 and skipped[0].path == right
    assert "also belongs" in skipped[0].reason
    assert not archive.exists()
