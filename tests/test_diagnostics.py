"""Offline, read-only diagnostic reports use the installed backend declarations."""
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import urllib.request

import pytest

import snug
import snug_runtime as runtime
from snug_core import ArchiveFormat, NativeBackend, StreamBackend

REAL_ACTIVATE = runtime.activate


class DiagnosticFFI:
    """Exported registration functions can still reject their feature."""

    def __init__(self):
        self.READ_FORMATS = {"7zip", "ar", "cab", "cpio", "iso9660", "lha", "rar", "rar5", "xar", "warc", "zip"}
        self.WRITE_FORMATS = {"ar_bsd", "cpio_newc"}
        self.READ_FILTERS = {"rpm", "gzip"}
        self.c_archive_p, self.c_int = object(), object()
        self.results, self.missing_symbols = {}, set()
        self.allocated, self.released, self.probed = [], [], []

    def version_number(self):
        return 3008001

    def _new(self, kind):
        handle = (kind, len(self.allocated))
        self.allocated.append(handle)
        return handle

    def read_new(self):
        return self._new("read")

    def write_new(self):
        return self._new("write")

    def read_free(self, handle):
        assert handle[0] == "read"
        self.released.append(handle)

    def write_free(self, handle):
        assert handle[0] == "write"
        self.released.append(handle)

    def ffi(self, name, arguments, result):
        assert arguments == [self.c_archive_p] and result is self.c_int
        if name in self.missing_symbols:
            raise AttributeError("native registration symbol is missing")
        def register(handle):
            assert handle in self.allocated and handle not in self.released
            self.probed.append((name, handle))
            outcome = self.results.get(name, 0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return register


class OptionalBackend:
    def __init__(self, name, library=None, available=True):
        self.name, self.library, self.installed = name, library, available
        self.read_formats = {ArchiveFormat.SEVEN_ZIP} if name == "py7zr" else {
            ArchiveFormat.RAR, ArchiveFormat.RAR5, ArchiveFormat.CPIO, ArchiveFormat.AR,
            ArchiveFormat.DEB, ArchiveFormat.RPM, ArchiveFormat.ISO,
        }
        self.write_formats = {ArchiveFormat.SEVEN_ZIP} if name == "py7zr" else {ArchiveFormat.CPIO, ArchiveFormat.AR}
        self._write_names = {ArchiveFormat.CPIO: "cpio_newc", ArchiveFormat.AR: "ar_bsd"}

    def available(self):
        os.environ["LIBARCHIVE"] = "incidental discovery mutation"
        return self.installed

    def _library(self):
        return self.library

    def can_write(self, fmt):
        if self.name == "libarchive":
            return {ArchiveFormat.CPIO: "cpio_newc", ArchiveFormat.AR: "ar_bsd"}.get(fmt) in self.library.ffi.WRITE_FORMATS
        return fmt in self.write_formats


@pytest.fixture
def libraries(monkeypatch):
    py7zr = SimpleNamespace(__version__="1.1.3", SevenZipFile=lambda *args: None, is_7zfile=lambda *args: True)
    ffi = DiagnosticFFI()
    libarchive = SimpleNamespace(ffi=ffi, file_reader=lambda *args: None, file_writer=lambda *args: None)
    modules = {"py7zr.io": SimpleNamespace(WriterFactory=type("WriterFactory", (), {})),
               "Cryptodome.Cipher.AES": SimpleNamespace(new=lambda *args: object())}
    original = importlib.import_module
    monkeypatch.setattr(importlib, "import_module", lambda name, *args: modules[name] if name in modules else original(name, *args))
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "5.3")
    return py7zr, libarchive, modules


@pytest.fixture
def backends(libraries):
    py7zr, libarchive, _ = libraries
    return [NativeBackend(), StreamBackend(), OptionalBackend("py7zr", py7zr), OptionalBackend("libarchive", libarchive)]


@pytest.fixture(autouse=True)
def no_network_or_updater(monkeypatch):
    import snug_runtime
    import snug_update
    def prohibited(*args, **kwargs):
        pytest.fail("diagnostics must not contact the network, repair, activate, or check updates")
    monkeypatch.setattr(urllib.request, "urlopen", prohibited)
    monkeypatch.setattr(urllib.request, "build_opener", prohibited)
    monkeypatch.setattr(snug_runtime, "check", prohibited)
    monkeypatch.setattr(snug_runtime, "activate", prohibited)
    monkeypatch.setattr(snug_runtime, "windows_repair", prohibited)
    monkeypatch.setattr(snug_update, "start_automatic_check", prohibited)
    monkeypatch.setattr(snug_update, "check_update", prohibited)
    monkeypatch.setattr(snug_update, "_managed_root", prohibited)
    monkeypatch.delenv("SNUG_MANAGED_ROOT", raising=False)


def managed_files(root, monkeypatch, *, windows=False):
    root.mkdir(exist_ok=True)
    lock = {"schema": 1, "binding": {"filename": "binding.whl", "url": "https://example.test/binding.whl", "sha256": "0" * 64}}
    (root / "runtime-lock.json").write_text(json.dumps(lock))
    for name in ("vendor", "packages", "native"):
        directory = root / name
        directory.mkdir()
        content = f"managed {name}".encode()
        (directory / "module.bin").write_bytes(content)
        (directory / ".snug-files.json").write_text(json.dumps({"module.bin": hashlib.sha256(content).hexdigest()}))
    if windows:
        (root / "runtime.ps1").write_text("existing Windows runtime")
        (root / "runtime.json").write_text(json.dumps({"kind": "windows", "python": "python.exe", "library": "native/archive.dll"}))
    else:
        (root / ".snug-install.json").write_text(json.dumps({"schema": 1, "kind": "homebrew", "branch": "main"}))
        monkeypatch.setenv("SNUG_MANAGED_ROOT", str(root))


def component(report, name):
    return next(item for item in report["backends"] if item["name"] == name)


def formats(report):
    return {row["format"]: row for row in report["formats"]}


def test_doctor_native_source_is_healthy_without_optional_backends(tmp_path, backends):
    for backend in backends[2:]:
        backend.installed = False
    report = snug._doctor_report(tmp_path, backends)
    assert report["schema_version"] == 1 and report["ok"] is True
    assert report["runtime"]["managed"] is False
    assert report["runtime"]["lock"]["status"] == "missing"
    assert component(report, "py7zr")["status"] == "unavailable"
    assert component(report, "libarchive")["required"] is False
    assert component(report, "native-stream")["required"] is True
    assert report["updater"]["ownership"] == "external"


def test_healthy_managed_doctor_is_deterministic_read_only(tmp_path, backends, monkeypatch):
    managed_files(tmp_path, monkeypatch)
    before = {path.relative_to(tmp_path): (path.read_bytes(), path.stat().st_mtime_ns) for path in tmp_path.rglob("*") if path.is_file()}
    monkeypatch.setenv("LIBARCHIVE", "original native library")
    first = snug._doctor_report(tmp_path, backends)
    assert first == snug._doctor_report(tmp_path, backends)
    assert first["ok"] is True
    assert first["updater"]["ownership"] == "managed"
    assert all(item["required"] for item in first["backends"])
    assert component(first, "py7zr")["required_api"] and component(first, "py7zr")["aes"]
    assert component(first, "libarchive")["binding_version"] == "5.3"
    after = {path.relative_to(tmp_path): (path.read_bytes(), path.stat().st_mtime_ns) for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before
    assert os.environ["LIBARCHIVE"] == "original native library"


@pytest.mark.parametrize("fault", ["py7zr-missing", "py7zr-version", "py7zr-api", "aes", "libarchive-missing", "native-version", "reader", "writer"])
def test_managed_required_backend_faults(tmp_path, backends, libraries, monkeypatch, fault):
    managed_files(tmp_path, monkeypatch)
    py7zr, libarchive, modules = libraries
    if fault == "py7zr-missing":
        backends[2].installed = False
    elif fault == "py7zr-version":
        py7zr.__version__ = "2.0.0"
    elif fault == "py7zr-api":
        modules["py7zr.io"] = SimpleNamespace()
    elif fault == "aes":
        modules["Cryptodome.Cipher.AES"].new = lambda *args: (_ for _ in ()).throw(OSError("AES extension cannot load"))
    elif fault == "libarchive-missing":
        backends[3].installed = False
    elif fault == "native-version":
        libarchive.ffi.version_number = lambda: 3007000
    elif fault == "reader":
        libarchive.ffi.READ_FORMATS.remove("rar")
    else:
        libarchive.ffi.WRITE_FORMATS.remove("cpio_newc")
    assert snug._doctor_report(tmp_path, backends)["ok"] is False


@pytest.mark.parametrize("fault", ["lock-missing", "lock-invalid", "binding-damaged", "inventory-missing", "package-missing", "traversal"])
def test_managed_runtime_integrity_faults(tmp_path, backends, monkeypatch, fault):
    managed_files(tmp_path, monkeypatch)
    if fault == "lock-missing":
        (tmp_path / "runtime-lock.json").unlink()
    elif fault == "lock-invalid":
        (tmp_path / "runtime-lock.json").write_text('{"schema":1,"binding":{}}')
    elif fault == "binding-damaged":
        (tmp_path / "vendor/module.bin").write_bytes(b"changed")
    elif fault == "inventory-missing":
        (tmp_path / "vendor/.snug-files.json").unlink()
    elif fault == "package-missing":
        (tmp_path / "packages/module.bin").unlink()
    else:
        (tmp_path / "vendor/.snug-files.json").write_text(json.dumps({"../outside": "0" * 64}))
    assert snug._doctor_report(tmp_path, backends)["ok"] is False


def test_windows_managed_runtime_is_separate_from_updater_ownership(tmp_path, backends, monkeypatch):
    managed_files(tmp_path, monkeypatch, windows=True)
    report = snug._doctor_report(tmp_path, backends)
    assert report["runtime"]["managed"] and report["runtime"]["kind"] == "windows"
    assert report["ok"] and report["updater"]["ownership"] == "external"
    (tmp_path / "runtime.json").write_text("broken state")
    report = snug._doctor_report(tmp_path, backends)
    assert report["runtime"]["managed"] and not report["ok"]


@pytest.mark.parametrize("kind", ["source", "pip", "custom"])
def test_doctor_external_ownership(tmp_path, backends, monkeypatch, kind):
    managed_files(tmp_path, monkeypatch)
    if kind == "source":
        (tmp_path / ".git").mkdir()
    elif kind == "pip":
        (tmp_path / "snug_archives.dist-info").mkdir()
    else:
        (tmp_path / ".snug-install.json").write_text('{"schema":1,"kind":"custom","branch":"external"}')
    assert snug._doctor_report(tmp_path, backends)["updater"]["ownership"] == "external"


def test_formats_native_only_reflects_declarations(tmp_path):
    rows = formats(snug._formats_report([NativeBackend(), StreamBackend()]))
    assert rows["zip"]["read"] and rows["zip"]["write"]
    assert rows["zip"]["read_backends"] == ["native"]
    assert rows["gz"]["read"] and rows["gz"]["write"]
    assert not rows["7z"]["read"] and not rows["7z"]["write"]
    assert not rows["rar"]["write"]


def test_formats_optional_readers_and_writers(backends, libraries):
    rows = formats(snug._formats_report(backends))
    assert rows["7z"]["write_backends"] == ["py7zr"]
    assert rows["rar"]["read_backends"] == ["libarchive"] and rows["rar"]["status"] == "read only"
    assert rows["cpio"]["write_backends"] == ["libarchive"]
    libraries[1].ffi.WRITE_FORMATS.remove("cpio_newc")
    libraries[1].ffi.READ_FORMATS.remove("iso9660")
    rows = formats(snug._formats_report(backends))
    assert rows["cpio"]["read"] and not rows["cpio"]["write"]
    assert not rows["iso"]["read"]


def test_formats_does_not_infer_rar5_from_rar_reader(backends, libraries):
    libraries[1].ffi.READ_FORMATS.remove("rar5")
    libraries[1].ffi.missing_symbols.add("read_support_format_rar5")
    rows = formats(snug._formats_report(backends))
    assert rows["rar"]["read"]
    assert not rows["rar5"]["read"]
    assert rows["rar5"]["details"] == ["libarchive reader unavailable"]


def test_native_zipx_capability_has_row_specific_codec_limit():
    row = formats(snug._formats_report([NativeBackend(), StreamBackend()]))["zipx"]
    assert row["read"] and not row["write"]
    assert "ZIP codecs" in row["details"][0]


@pytest.mark.parametrize("outcome", ["supported", "missing", "failed"])
def test_rar5_uses_native_reader_evidence(backends, libraries, outcome):
    ffi = libraries[1].ffi
    ffi.READ_FORMATS.remove("rar5")
    if outcome == "missing":
        ffi.missing_symbols.add("read_support_format_rar5")
    elif outcome == "failed":
        ffi.results["read_support_format_rar5"] = -30
    row = formats(snug._formats_report(backends))["rar5"]
    assert row["read"] is (outcome == "supported")
    assert ffi.released == ffi.allocated


@pytest.mark.parametrize("kind,name", [("reader", "iso9660"), ("writer", "cpio_newc"), ("filter", "rpm")])
@pytest.mark.parametrize("outcome", [-20, -30, RuntimeError("native registration failed")])
def test_formats_require_successful_native_registration(backends, libraries, kind, name, outcome):
    ffi = libraries[1].ffi
    prefix = {"reader": "read_support_format_", "writer": "write_set_format_", "filter": "read_support_filter_"}[kind]
    ffi.results[prefix + name] = outcome
    rows = formats(snug._formats_report(backends))
    if kind == "reader":
        assert not rows["iso"]["read"]
    elif kind == "writer":
        assert rows["cpio"]["read"] and not rows["cpio"]["write"]
    else:
        assert not rows["rpm"]["read"] and rows["cpio"]["read"]
    assert ffi.allocated == ffi.released
    assert len({handle for _, handle in ffi.probed}) == len(ffi.probed)


@pytest.mark.parametrize("kind", ["reader", "writer", "filter"])
def test_native_registration_null_handle_and_bookkeeping_skip(libraries, kind):
    ffi = libraries[1].ffi
    assert not snug._diagnostic_registration(ffi, kind, "all")
    assert not snug._diagnostic_registration(ffi, kind, "bad/name")
    assert not ffi.allocated
    if kind == "writer":
        ffi.write_new = lambda: None
    else:
        ffi.read_new = lambda: None
    assert not snug._diagnostic_registration(ffi, kind, "zip")
    assert not ffi.probed and not ffi.released


def test_native_diagnostic_registration_sets_and_managed_health(tmp_path, backends, libraries, monkeypatch):
    ffi = libraries[1].ffi
    ffi.READ_FORMATS.add("all")
    ffi.READ_FILTERS.add("all")
    ffi.results["read_support_format_rar"] = -20
    ffi.results["write_set_format_ar_bsd"] = -30
    ffi.results["read_support_filter_gzip"] = RuntimeError("codec missing")
    managed_files(tmp_path, monkeypatch)
    report = snug._doctor_report(tmp_path, backends)
    native = component(report, "libarchive")
    assert not report["ok"] and not native["managed_compatible"]
    assert "rar" not in native["native_readers"] and "all" not in native["native_readers"]
    assert "ar_bsd" not in native["native_writers"]
    assert native["native_filters"] == ["rpm"]
    assert ffi.allocated == ffi.released
    assert all(not name.endswith("_all") for name, _ in ffi.probed)


@pytest.mark.parametrize("kind,prefix", [("reader", "read_support_format_"),
                                        ("writer", "write_set_format_"),
                                        ("filter", "read_support_filter_")])
@pytest.mark.parametrize("result", [0, -20])
def test_native_registration_avoids_binding_warning_handlers(libraries, monkeypatch, kind, prefix, result):
    import ctypes
    ffi = libraries[1].ffi
    ffi.libarchive = object()
    ffi.ffi = lambda *args: pytest.fail("raw registration must not mutate binding handlers")
    def callable_type(result_type, handle_type):
        assert result_type is ffi.c_int and handle_type is ffi.c_archive_p
        def resolve(symbol):
            assert symbol == ("archive_" + prefix + "zip", ffi.libarchive)
            return lambda handle: result
        return resolve
    monkeypatch.setattr(ctypes, "CFUNCTYPE", callable_type)
    assert snug._diagnostic_registration(ffi, kind, "zip") is (result == 0)
    assert ffi.allocated == ffi.released


def test_formats_reports_optional_absence_and_restores_environment(backends, monkeypatch):
    monkeypatch.delenv("LIBARCHIVE", raising=False)
    backends[2].installed = False
    backends[3].installed = False
    rows = formats(snug._formats_report(backends))
    assert rows["7z"]["details"] == ["py7zr unavailable"]
    assert rows["rar"]["details"] == ["libarchive unavailable"]
    assert "LIBARCHIVE" not in os.environ


@pytest.mark.parametrize("command", ["doctor", "formats"])
@pytest.mark.parametrize("structured", [False, True])
def test_diagnostic_command_output_and_no_engine(tmp_path, backends, monkeypatch, capsys, command, structured):
    report = snug._doctor_report(tmp_path, backends) if command == "doctor" else snug._formats_report(backends)
    monkeypatch.setattr(snug, f"_{command}_report", lambda: report)
    monkeypatch.setattr(snug, "ArchiveEngine", lambda: pytest.fail("diagnostics do not need an archive engine"))
    assert getattr(snug, f"_cmd_{command}")(SimpleNamespace(json=structured)) == 0
    output = capsys.readouterr()
    assert not output.err
    if structured:
        assert json.loads(output.out) == report
    else:
        assert "Snug" in output.out if command == "doctor" else "Format" in output.out


@pytest.mark.parametrize("command", ["doctor", "formats"])
def test_cli_diagnostics_skip_updates_and_archive_engine(tmp_path, backends, monkeypatch, capsys, command):
    report = snug._doctor_report(tmp_path, backends) if command == "doctor" else snug._formats_report(backends)
    monkeypatch.setattr(snug, f"_{command}_report", lambda: report)
    monkeypatch.setattr(snug, "ArchiveEngine", lambda: pytest.fail("diagnostics must not instantiate ArchiveEngine"))
    assert snug.main([command, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == report


def test_human_diagnostic_backend_names_are_escaped(backends, monkeypatch, capsys):
    backends[2].name = "backend\x1b[31m\x07"
    report = snug._formats_report(backends)
    monkeypatch.setattr(snug, "_formats_report", lambda: report)
    snug._cmd_formats(SimpleNamespace(json=False))
    text = capsys.readouterr().out
    assert "\x1b" not in text and "\x07" not in text


def test_runtime_activation_can_skip_corrupt_state(tmp_path, monkeypatch):
    (tmp_path / "runtime.json").write_text("[]")
    monkeypatch.setattr(runtime.sys, "path", list(sys.path))
    monkeypatch.delenv("SNUG_LIBRARY", raising=False)
    monkeypatch.delenv("LIBARCHIVE", raising=False)
    REAL_ACTIVATE(tmp_path, read_state=False)
    assert str(tmp_path / "vendor") in runtime.sys.path
    assert "LIBARCHIVE" not in os.environ
    assert (tmp_path / "runtime.json").read_text() == "[]"


@pytest.mark.parametrize("command", ["doctor", "formats"])
def test_runtime_diagnostic_dispatch_skips_probe_and_state(command, monkeypatch):
    calls = []
    monkeypatch.setattr(runtime, "activate", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(runtime.sys, "argv", ["snug_runtime.py", "--run", command, "--json"])
    monkeypatch.setattr(snug, "main", lambda args: calls.append(args) or 0)
    assert runtime.main() == 0
    assert calls == [{"read_state": False}, [command, "--json"]]


@pytest.mark.skipif(os.name == "nt", reason="Bash/Homebrew launcher is Unix-only")
@pytest.mark.parametrize("command", ["doctor", "formats"])
@pytest.mark.parametrize("healthy", [True, False])
def test_unix_launcher_diagnostics_are_offline_without_runtime_repair(tmp_path, command, healthy):
    repository = Path(snug.__file__).parent
    app = tmp_path / "managed app Tiếng Việt 📦"
    app.mkdir()
    for name in ("snug.py", "snug_core.py", "snug_ext.py", "snug_runtime.py", "snug_update.py", "runtime.sh", "runtime-lock.json"):
        shutil.copyfile(repository / name, app / name)
    (app / ".snug-install.json").write_text('{"schema":1,"kind":"homebrew","branch":"main"}')
    # State is deliberately an unusable list. Doctor reports it; activation must
    # not parse it before reaching the diagnostic command.
    (app / "runtime.json").write_text("[]")
    vendor = app / "vendor"
    packages = app / "packages"
    sources = {
        vendor / "libarchive/__init__.py": ("raise OSError('fixture broken native library')\n" if not healthy else
            "from types import SimpleNamespace\n"
            "ffi = SimpleNamespace(version_number=lambda:3008001, READ_FORMATS={'7zip','ar','cab','cpio','iso9660','lha','rar','rar5','xar','warc','zip'}, WRITE_FORMATS={'ar_bsd','cpio_newc'}, READ_FILTERS={'rpm'}, c_archive_p=object(), c_int=object(), read_new=lambda:object(), write_new=lambda:object(), read_free=lambda handle:None, write_free=lambda handle:None, ffi=lambda *args:lambda handle:0)\n"
            "def file_reader(*args): pass\n"
            "def file_writer(*args): pass\n"),
        packages / "py7zr/__init__.py": ("raise ImportError('fixture missing py7zr')\n" if not healthy else
            "__version__='1.1.3'\n"
            "def SevenZipFile(*args): pass\n"
            "def is_7zfile(*args): return True\n"),
        packages / "py7zr/io.py": "class WriterFactory: pass\n",
        packages / "Cryptodome/__init__.py": "",
        packages / "Cryptodome/Cipher/__init__.py": "",
        packages / "Cryptodome/Cipher/AES.py": "def new(*args): return object()\n",
    }
    for path, content in sources.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for directory in (vendor, packages):
        record = {path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in directory.rglob("*") if path.is_file()}
        (directory / ".snug-files.json").write_text(json.dumps(record))
    brew_root = tmp_path / "brew"
    python = brew_root / "opt/py7zr/libexec/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/usr/bin/env bash\nexec "$SNUG_TEST_PYTHON" "$@"\n')
    python.chmod(0o755)
    # No native library exists: resolve_runtime fails, but Python still resolves.
    brew = tmp_path / "offline-brew"
    brew.write_text('#!/usr/bin/env bash\n[[ "$1" == --prefix && "$#" == 1 ]] || exit 91\nprintf "%s\\n" "$SNUG_TEST_BREW_ROOT"\n')
    brew.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ, HOME=str(home), SNUG_BREW=str(brew), SNUG_TEST_BREW_ROOT=str(brew_root),
               SNUG_TEST_PYTHON=sys.executable, SNUG_NO_UPDATE_CHECK="1", PYTHONDONTWRITEBYTECODE="1")
    env.pop("LIBARCHIVE", None)
    env.pop("PYTHONPATH", None)
    before = {path.relative_to(app): path.read_bytes() for path in app.rglob("*") if path.is_file()}
    result = subprocess.run(["bash", str(app / "runtime.sh"), str(app), "--run", command, "--json"],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == (2 if command == "doctor" and not healthy else 0), result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert not result.stderr and "Traceback" not in result.stdout
    assert report["schema_version"] == 1
    if command == "doctor":
        assert report["ok"] is healthy
        assert report["runtime"]["state"]["status"] == "corrupt"
    after = {path.relative_to(app): path.read_bytes() for path in app.rglob("*") if path.is_file()}
    assert after == before and not list(home.rglob("*"))
    assert not (app / ".repair-lock").exists()
