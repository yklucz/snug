"""Real native payload checks, structural validation, and CLI integrity behavior."""

import builtins
import bz2
import gzip
import io
import lzma
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import warnings
import zipfile

import pytest

import snug
import snug_core as core


FORMATS = ["zip", "tar", "tar.gz", "gz", "bz2", "xz", "lzma"]
STREAM_FORMATS = {"gz", "bz2", "xz", "lzma"}
PAYLOAD = bytes(range(256)) * (core.CHUNK_SIZE // 128 + 1)
SCRIPT = Path(__file__).resolve().parents[1] / "snug.py"


@pytest.fixture
def native_engine():
    # Keep these checks independent of optional decoder fallbacks.
    return core.ArchiveEngine([core.NativeBackend(), core.StreamBackend()])


def archive_fixture(tmp_path, suffix, payload=PAYLOAD, name="payload.txt", *, extras=False):
    archive = tmp_path / (f"payload.txt.{suffix}" if suffix in STREAM_FORMATS
                          else f"archive.{suffix}")
    if suffix == "zip":
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as handle:
            if extras:
                handle.writestr("directory/", b"")
            handle.writestr(name, payload)
            if extras:
                handle.writestr("empty.txt", b"")
    elif suffix in {"tar", "tar.gz"}:
        with tarfile.open(archive, "w:gz" if suffix == "tar.gz" else "w") as handle:
            if extras:
                directory = tarfile.TarInfo("directory")
                directory.type = tarfile.DIRTYPE
                handle.addfile(directory)
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            handle.addfile(member, io.BytesIO(payload))
            if extras:
                handle.addfile(tarfile.TarInfo("empty.txt"), io.BytesIO(b""))
    else:
        codec = {"gz": gzip, "bz2": bz2, "xz": lzma, "lzma": lzma}[suffix]
        options = {"format": lzma.FORMAT_ALONE} if suffix == "lzma" else {}
        archive.write_bytes(codec.compress(payload, **options))
    return archive


def corrupt_zip_payload(archive, name="payload.txt"):
    with zipfile.ZipFile(archive) as handle:
        member = handle.getinfo(name)
        offset = member.header_offset + 30 + len(member.filename.encode()) + len(member.extra)
    damaged = bytearray(archive.read_bytes())
    damaged[offset + member.file_size // 2] ^= 0xFF
    archive.write_bytes(damaged)


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
            for path in root.rglob("*")}


def deny_filesystem_writes(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("integrity testing must not create or modify filesystem output")

    def guarded_opener(opener):
        def open_readonly(file, mode="r", *args, **kwargs):
            if isinstance(mode, str) and any(flag in mode for flag in "wax+"):
                forbidden()
            return opener(file, mode, *args, **kwargs)
        return open_readonly

    monkeypatch.setattr(builtins, "open", guarded_opener(builtins.open))
    monkeypatch.setattr(io, "open", guarded_opener(io.open))
    original_os_open = os.open

    def open_readonly(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            forbidden()
        return original_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_readonly)
    for operation in ("mkdir", "unlink", "remove", "rename", "replace", "link", "symlink"):
        monkeypatch.setattr(os, operation, forbidden)
    monkeypatch.setattr(core.tempfile, "mkstemp", forbidden)
    monkeypatch.setattr(core.SafeOutputFile, "__init__", forbidden)


class RecordingProgress:
    def __init__(self):
        self.started = []
        self.names = []
        self.bytes = 0
        self.finished = False

    def start(self, total_bytes, total_items):
        self.started.append((total_bytes, total_items))

    def item(self, name):
        self.names.append(name)

    def chunk(self, length):
        self.bytes += length

    def done(self):
        self.finished = True


@pytest.mark.parametrize("suffix", FORMATS)
def test_integrity_streams_real_payload_and_never_writes_output(
    native_engine, tmp_path, monkeypatch, suffix,
):
    archive = archive_fixture(tmp_path, suffix, extras=suffix not in STREAM_FORMATS)
    (tmp_path / "existing.txt").write_bytes(b"previous user data")
    before = snapshot(tmp_path)
    progress = RecordingProgress()
    with monkeypatch.context() as patch:
        deny_filesystem_writes(patch)
        report = native_engine.test(archive, progress=progress)
    assert isinstance(report, core.TestReport)
    assert report.archive == archive
    assert report.format.value == suffix
    assert report.entries == (1 if suffix in STREAM_FORMATS else 3)
    assert report.files == (1 if suffix in STREAM_FORMATS else 2)
    assert report.bytes_read == len(PAYLOAD)
    assert report.elapsed >= 0
    assert progress.bytes == len(PAYLOAD)
    assert len(progress.started) == 1 and progress.finished
    assert progress.names
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("suffix", FORMATS)
def test_integrity_accepts_empty_regular_payload(native_engine, tmp_path, suffix):
    archive = archive_fixture(tmp_path, suffix, payload=b"")
    report = native_engine.test(archive)
    assert (report.entries, report.files, report.bytes_read) == (1, 1, 0)


@pytest.mark.parametrize("suffix", ["zip", "tar", "tar.gz"])
def test_integrity_accepts_empty_container(native_engine, tmp_path, suffix):
    archive = tmp_path / f"empty.{suffix}"
    if suffix == "zip":
        with zipfile.ZipFile(archive, "w"):
            pass
    else:
        with tarfile.open(archive, "w:gz" if suffix == "tar.gz" else "w"):
            pass
    report = native_engine.test(archive)
    assert (report.entries, report.files, report.bytes_read) == (0, 0, 0)


def test_integrity_native_zip_uses_bounded_reads(native_engine, tmp_path, monkeypatch):
    archive = archive_fixture(tmp_path, "zip")
    original = zipfile.ZipExtFile.read
    sizes = []

    def bounded_read(handle, size=-1):
        sizes.append(size)
        assert 0 < size <= core.CHUNK_SIZE
        return original(handle, size)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", bounded_read)
    assert native_engine.test(archive).bytes_read == len(PAYLOAD)
    assert len(sizes) >= 3


@pytest.mark.parametrize("suffix", sorted(STREAM_FORMATS))
@pytest.mark.parametrize("operation", ["test", "extract"])
def test_stream_payload_is_decoded_once_without_a_preflight_pass(tmp_path, monkeypatch, suffix, operation):
    archive = archive_fixture(tmp_path, suffix)
    backend = core.StreamBackend()
    open_stream = backend._open
    reads = []

    def counted_open(path, fmt, mode, level=None):
        if "r" in mode:
            reads.append(path)
        return open_stream(path, fmt, mode, level)

    monkeypatch.setattr(backend, "_open", counted_open)
    engine = core.ArchiveEngine([backend])
    if operation == "test":
        assert engine.test(archive).bytes_read == len(PAYLOAD)
    else:
        root = tmp_path / "output"
        assert engine.extract(archive, root).bytes_written == len(PAYLOAD)
        assert (root / "payload.txt").read_bytes() == PAYLOAD
    assert reads == [archive]


def test_integrity_inspects_native_archive_once(tmp_path, monkeypatch):
    archive = archive_fixture(tmp_path, "zip", payload=b"content")
    backend = core.NativeBackend()
    original = backend.inspect
    calls = []

    def counted(path, password=None):
        calls.append((path, password))
        return original(path, password=password)

    monkeypatch.setattr(backend, "inspect", counted)
    report = core.ArchiveEngine([backend]).test(archive)
    assert report.bytes_read == 7
    assert calls == [(archive, None)]


def test_integrity_crc_error_identifies_member_and_keeps_disk_unchanged(
    native_engine, tmp_path, monkeypatch,
):
    archive = archive_fixture(tmp_path, "zip")
    corrupt_zip_payload(archive)
    before = snapshot(tmp_path)
    with monkeypatch.context() as patch:
        deny_filesystem_writes(patch)
        with pytest.raises(core.ArchiveError, match="payload.txt"):
            native_engine.test(archive)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("suffix", FORMATS)
def test_integrity_rejects_truncated_payload_or_container(native_engine, tmp_path, suffix):
    archive = archive_fixture(tmp_path, suffix)
    if suffix == "tar":
        # A header still declares the full member, but its bytes are absent.
        archive.write_bytes(archive.read_bytes()[:512 + 123])
    elif suffix == "zip":
        archive.write_bytes(archive.read_bytes()[:core.CHUNK_SIZE + 73])
    else:
        archive.write_bytes(archive.read_bytes()[:-16])
    with pytest.raises(core.ArchiveError):
        native_engine.test(archive)


def test_integrity_rejects_zip_missing_only_central_directory(native_engine, tmp_path):
    archive = archive_fixture(tmp_path, "zip", payload=b"otherwise complete payload")
    with zipfile.ZipFile(archive) as handle:
        central_start = handle.start_dir
    archive.write_bytes(archive.read_bytes()[:central_start])
    with pytest.raises(core.ArchiveError):
        native_engine.test(archive)


def test_integrity_drains_tar_gzip_footer(native_engine, tmp_path):
    archive = archive_fixture(tmp_path, "tar.gz", payload=b"payload before TAR end marker")
    damaged = bytearray(archive.read_bytes())
    damaged[-8] ^= 0xFF
    archive.write_bytes(damaged)
    with pytest.raises(core.ArchiveError):
        native_engine.test(archive)


@pytest.mark.parametrize("suffix,reader", [("zip", zipfile.ZipExtFile),
                                           ("tar", tarfile.ExFileObject),
                                           ("gz", gzip.GzipFile)])
def test_integrity_decoder_error_identifies_member(native_engine, tmp_path, monkeypatch, suffix, reader):
    archive = archive_fixture(tmp_path, suffix, payload=b"content")

    def failed_read(*args, **kwargs):
        raise OSError("controlled payload read failure")

    monkeypatch.setattr(reader, "read", failed_read)
    with pytest.raises(core.ArchiveError, match="payload.txt"):
        native_engine.test(archive)


def test_integrity_rejects_late_archive_close_failure(native_engine, tmp_path, monkeypatch):
    archive = archive_fixture(tmp_path, "zip", payload=b"content")
    progress = RecordingProgress()
    original = zipfile.ZipFile.__exit__

    def failed_exit(handle, *args):
        original(handle, *args)
        if progress.bytes:
            raise OSError("controlled archive close failure")

    monkeypatch.setattr(zipfile.ZipFile, "__exit__", failed_exit)
    with pytest.raises(core.ArchiveError, match="archive close failure"):
        native_engine.test(archive, progress=progress)
    assert not progress.finished
    assert list(tmp_path.iterdir()) == [archive]


def test_integrity_interrupt_closes_reader_and_leaves_no_outputs(
    native_engine, tmp_path, monkeypatch,
):
    archive = archive_fixture(tmp_path, "zip")
    before = snapshot(tmp_path)
    closed = []
    original_close = zipfile.ZipExtFile.close
    progress = RecordingProgress()

    def interrupted_read(*args, **kwargs):
        raise KeyboardInterrupt

    def tracked_close(handle):
        closed.append(handle)
        return original_close(handle)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", interrupted_read)
    monkeypatch.setattr(zipfile.ZipExtFile, "close", tracked_close)
    with pytest.raises(KeyboardInterrupt):
        native_engine.test(archive, progress=progress)
    assert closed and all(handle.closed for handle in closed)
    assert not progress.finished
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("suffix", ["zip", "tar"])
@pytest.mark.parametrize("name", ["../outside.txt", "/absolute.txt", "C:relative.txt",
                                  "C:/absolute.txt", "\\\\server\\share\\file.txt",
                                  "folder/../../outside.txt"])
def test_integrity_rejects_unsafe_member_names_without_outputs(
    native_engine, tmp_path, suffix, name,
):
    archive = archive_fixture(tmp_path, suffix, payload=b"content", name=name)
    before = snapshot(tmp_path)
    with pytest.raises(core.UnsafeArchiveError):
        native_engine.test(archive)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("names", [("same.txt", "same.txt"), ("same.txt", "./same.txt"),
                                   ("folder/file.txt", "folder\\file.txt"),
                                   ("folder/file.txt", "folder//file.txt"),
                                   ("parent", "parent/child.txt"),
                                   ("parent/child.txt", "parent")])
@pytest.mark.parametrize("suffix", ["zip", "tar"])
def test_integrity_rejects_duplicate_outputs_and_non_directory_parents(
    native_engine, tmp_path, names, suffix,
):
    archive = tmp_path / f"duplicates.{suffix}"
    if suffix == "zip":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(archive, "w") as handle:
                for name in names:
                    handle.writestr(name, b"content")
    else:
        with tarfile.open(archive, "w") as handle:
            for name in names:
                member = tarfile.TarInfo(name)
                member.size = 7
                handle.addfile(member, io.BytesIO(b"content"))
    with pytest.raises(core.UnsafeArchiveError):
        native_engine.test(archive)
    assert list(tmp_path.iterdir()) == [archive]


def test_integrity_structure_rejects_nul_before_any_decoder(tmp_path, monkeypatch):
    archive = archive_fixture(tmp_path, "zip", payload=b"content")
    backend = core.NativeBackend()
    monkeypatch.setattr(backend, "inspect", lambda *args, **kwargs: core.ArchiveInspection(
        [core.ArchiveEntry("payload\0hidden.txt", size=7)], {}))

    def forbidden_decode(*args, **kwargs):
        pytest.fail("unsafe inspection must fail before payload decoding")

    monkeypatch.setattr(backend, "test", forbidden_decode)
    with pytest.raises(core.UnsafeArchiveError):
        core.ArchiveEngine([backend]).test(archive)


@pytest.mark.parametrize("component", ["CON", "con.txt", "PRN", "AUX", "NUL",
                                       "COM1.txt", "LPT9", "COM¹.txt", "LPT²", "COM³",
                                       "CONIN$", "CONOUT$", "file:stream", "trailing.", "trailing "])
def test_windows_integrity_path_rules_reject_reserved_aliases_and_streams(component):
    # Exercise Windows validation on every host without changing global os.name.
    with pytest.raises(core.UnsafeArchiveError):
        core._validate_windows_parts(["directory", component], f"directory/{component}")


@pytest.mark.parametrize("component", ["console.txt", "COM10.txt", "LPT0", "normal.txt"])
def test_windows_integrity_path_rules_allow_regular_names(component):
    core._validate_windows_parts([component], component)


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE])
@pytest.mark.parametrize("target", ["../outside.txt", "/outside.txt", "C:/outside.txt"])
def test_integrity_rejects_escaping_tar_links(native_engine, tmp_path, kind, target):
    archive = tmp_path / "links.tar"
    with tarfile.open(archive, "w") as handle:
        link = tarfile.TarInfo("link")
        link.type, link.linkname = kind, target
        handle.addfile(link)
    with pytest.raises(core.UnsafeArchiveError):
        native_engine.test(archive)


def test_integrity_valid_tar_links_require_no_link_creation(native_engine, tmp_path, monkeypatch):
    archive = tmp_path / "links.tar"
    with tarfile.open(archive, "w") as handle:
        payload = tarfile.TarInfo("payload.txt")
        payload.size = 7
        handle.addfile(payload, io.BytesIO(b"content"))
        for name, kind in [("symlink", tarfile.SYMTYPE), ("hardlink", tarfile.LNKTYPE)]:
            member = tarfile.TarInfo(name)
            member.type, member.linkname = kind, "payload.txt"
            handle.addfile(member)
    with monkeypatch.context() as patch:
        deny_filesystem_writes(patch)
        report = native_engine.test(archive)
    assert report.entries == 3
    assert report.bytes_read == 7


def run_cli(*args, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], cwd=cwd,
                          env=dict(os.environ, SNUG_NO_UPDATE_CHECK="1", PYTHONIOENCODING="utf-8"),
                          capture_output=True, text=True, encoding="utf-8", timeout=45)


def test_cli_integrity_reports_success_without_outputs(tmp_path):
    archive = archive_fixture(tmp_path, "zip", payload=b"content", extras=True)
    before = snapshot(tmp_path)
    result = run_cli("test", archive, cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Archive is OK." in result.stdout
    assert "Traceback" not in result.stderr
    assert snapshot(tmp_path) == before


def test_cli_integrity_crc_error_identifies_member(tmp_path):
    archive = archive_fixture(tmp_path, "zip", payload=b"content")
    corrupt_zip_payload(archive)
    result = run_cli("test", archive)
    assert result.returncode == 2
    assert "payload.txt" in result.stderr
    assert "error:" in result.stderr and "Traceback" not in result.stderr
    assert "Archive is OK." not in result.stdout


def test_cli_integrity_unsafe_structure_has_security_exit_code(tmp_path):
    archive = archive_fixture(tmp_path, "zip", payload=b"content", name="../outside.txt")
    result = run_cli("test", archive)
    assert result.returncode == 3
    assert "unsafe archive" in result.stderr and "Traceback" not in result.stderr
    assert not (tmp_path.parent / "outside.txt").exists()


@pytest.mark.parametrize("corrupt", [False, True])
def test_cli_integrity_escapes_member_control_text(tmp_path, corrupt):
    name = "hello\x1b[31mred\x07\rname\n.txt"
    archive = archive_fixture(tmp_path, "zip", payload=b"content", name=name)
    if corrupt:
        corrupt_zip_payload(archive, name)
    result = run_cli("test", archive)
    assert result.returncode == (2 if corrupt else 0), result.stdout + result.stderr
    assert all(char not in result.stdout + result.stderr for char in ("\x1b", "\x07", "\r"))
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("password", [None, "incorrect", "fixture-password"])
def test_integrity_native_zip_password_conventions(native_engine, fixture_dir, password):
    archive = fixture_dir / "sample-traditional.zip"
    if password == "fixture-password":
        report = native_engine.test(archive, password=password)
        assert report.files == 1 and report.bytes_read == len(b"ZIP password fixture\n")
    else:
        with pytest.raises(core.ArchiveError, match=r"(?i)password"):
            native_engine.test(archive, password=password)


def test_cli_integrity_password_file_does_not_expose_secret(tmp_path, fixture_dir):
    password = "fixture-password"
    password_file = tmp_path / "password.txt"
    password_file.write_text(password + "\n", encoding="utf-8")
    password_file.chmod(0o600)
    result = run_cli("test", fixture_dir / "sample-traditional.zip", "--password-file", password_file)
    assert result.returncode == 0, result.stdout + result.stderr
    assert password not in result.stdout + result.stderr
    assert "Archive is OK." in result.stdout


def test_cli_integrity_password_uses_secure_prompt(monkeypatch, capsys, fixture_dir):
    calls = []

    def prompt(label):
        calls.append(label)
        return "fixture-password"

    monkeypatch.setenv("SNUG_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(snug.getpass, "getpass", prompt)
    assert snug.main(["test", str(fixture_dir / "sample-traditional.zip"), "--password"]) == 0
    captured = capsys.readouterr()
    assert len(calls) == 1
    assert "fixture-password" not in captured.out + captured.err


def test_cli_integrity_password_prompt_refuses_echo_fallback(monkeypatch, capsys, fixture_dir):
    def insecure_prompt(label):
        warnings.warn("echo fallback", snug.getpass.GetPassWarning)
        return "must never be read"

    monkeypatch.setenv("SNUG_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(snug.getpass, "getpass", insecure_prompt)
    assert snug.main(["test", str(fixture_dir / "sample-traditional.zip"), "--password"]) == 2
    captured = capsys.readouterr()
    assert "cannot securely prompt" in captured.err
    assert "must never be read" not in captured.out + captured.err


@pytest.mark.parametrize("args", [("test",), ("test", "missing.zip"),
                                  ("test", "missing.zip", "--password", "literal-secret"),
                                  ("test", "missing.zip", "--passw"),
                                  ("test", "missing.zip", "--password", "--password-file", "file")])
def test_cli_integrity_rejects_invalid_arguments(args):
    result = run_cli(*args)
    assert result.returncode == 2
    assert "error:" in result.stderr and "Traceback" not in result.stderr
