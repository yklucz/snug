from pathlib import Path
import zipfile

import pytest

import snug
from snug_core import ArchiveInspection


@pytest.fixture
def quiet_tui(monkeypatch):
    monkeypatch.setattr(snug, "_clear_screen", lambda: None)
    monkeypatch.setattr(snug, "_wait_for_enter", lambda: None)
    monkeypatch.setattr(snug, "ProgressDisplay", lambda *args, **kwargs: snug.NullProgress())


def script_menus(monkeypatch, choices, *, password=None):
    actions = iter(choices)
    seen = []

    def select(title, options, subtitle=None):
        seen.append((title, options, subtitle))
        if password:
            assert password not in str(options)
        value = next(actions)
        if callable(value):
            value = value(title, options)
        assert value is None or value in dict(options), (title, value, options)
        return value

    monkeypatch.setattr(snug, "_select_menu", select)
    return seen


def script_prompts(monkeypatch, responses):
    values = iter(responses)
    monkeypatch.setattr(snug, "_prompt", lambda *args: next(values))


def choose_format(name):
    return lambda title, options: next(key for key, label in options if label == name)


def make_zip(path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("tree/one.txt", b"one")
        archive.writestr("tree/two.txt", b"second")
    return path


def test_create_forwards_review_options_and_uses_selected_enumeration_policy(
        monkeypatch, tmp_path, quiet_tui, capsys):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "hello\x1b[31m.txt"
    monkeypatch.setattr(snug, "_select_sources_arrow", lambda cwd: [source])
    password_file = tmp_path / "password.txt"
    secret = "  secret password  "
    password_file.write_text(secret + "\n")
    menus = script_menus(monkeypatch, ["f", "2", "l", "p", "f", "y", "k", "o", "r"], password=secret)
    script_prompts(monkeypatch, ["7", str(password_file), "bundle.7z"])
    captured = {}

    def enumerate_sources(sources, **kwargs):
        captured["enumeration"] = (sources, kwargs)
        return ([], 0)

    monkeypatch.setattr(snug, "_interactive_enumerate", enumerate_sources)

    class Engine:
        def writable_formats(self):
            return [snug.ArchiveFormat.ZIP, snug.ArchiveFormat.SEVEN_ZIP]

        def create(self, archive, sources, **kwargs):
            captured["create"] = (archive, sources, kwargs)
            return snug.CreateReport(archive, kwargs["fmt"])

    snug._menu_create(Engine())
    archive, sources, options = captured["create"]
    assert archive == tmp_path / "bundle.7z"
    assert sources == [source]
    assert options["fmt"] is snug.ArchiveFormat.SEVEN_ZIP
    assert options["compresslevel"] == 7
    assert options["symlinks"] == "skip"
    assert options["password"] == secret
    assert options["root"] == tmp_path
    assert options["precollected"] == ([], 0)
    assert captured["enumeration"][1]["symlinks"] == "skip"
    assert captured["enumeration"][1]["archive_real"] == str(archive)
    labels = dict(menus[0][1])
    assert "\\x1b" in labels["s"] and "\x1b" not in labels["s"]
    assert secret not in capsys.readouterr().out


def test_create_review_only_offers_installed_writers(monkeypatch, tmp_path, quiet_tui):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(snug, "_select_sources_arrow", lambda cwd: [tmp_path / "one.txt"])
    menus = script_menus(monkeypatch, ["f", "b", "b"])
    engine = snug.ArchiveEngine(backends=[snug.NativeBackend()])
    snug._menu_create(engine)
    offered = dict(menus[1][1])
    assert set(offered.values()) == {value.value for value in engine.writable_formats()} | {"Back"}
    assert "7z" not in offered.values()


def test_create_unsupported_password_does_not_prompt_insecurely(monkeypatch, tmp_path, quiet_tui):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(snug, "_select_sources_arrow", lambda cwd: [tmp_path / "one.txt"])
    menus = script_menus(monkeypatch, ["p", "b"])
    monkeypatch.setattr(snug, "_read_password", lambda args: pytest.fail("unsupported writer prompted"))
    snug._menu_create(snug.ArchiveEngine(backends=[snug.NativeBackend()]))
    assert "unavailable" in dict(menus[0][1])["p"]


@pytest.mark.parametrize("cancel", [None, "b"])
def test_create_review_cancel_never_enumerates_or_writes(monkeypatch, tmp_path, quiet_tui, cancel):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(snug, "_select_sources_arrow", lambda cwd: [tmp_path / "one.txt"])
    script_menus(monkeypatch, [cancel])
    monkeypatch.setattr(snug, "_interactive_enumerate", lambda *a, **k: pytest.fail("cancel enumerated"))
    snug._menu_create(snug.ArchiveEngine())
    assert list(tmp_path.iterdir()) == []


def test_create_existing_output_decline_preserves_archive(monkeypatch, tmp_path, quiet_tui):
    monkeypatch.chdir(tmp_path)
    archive = tmp_path / "archive.tar.gz"
    archive.write_bytes(b"existing archive")
    monkeypatch.setattr(snug, "_select_sources_arrow", lambda cwd: [tmp_path / "one.txt"])
    script_menus(monkeypatch, ["r"])
    script_prompts(monkeypatch, ["n"])
    monkeypatch.setattr(snug, "_interactive_enumerate", lambda *a, **k: pytest.fail("decline enumerated"))
    snug._menu_create(snug.ArchiveEngine())
    assert archive.read_bytes() == b"existing archive"


def test_create_enumeration_cancel_writes_no_archive(monkeypatch, tmp_path, quiet_tui):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(snug, "_select_sources_arrow", lambda cwd: [tmp_path / "one.txt"])
    script_menus(monkeypatch, ["r"])
    monkeypatch.setattr(snug, "_interactive_enumerate", lambda *a, **k: None)
    snug._menu_create(snug.ArchiveEngine())
    assert not (tmp_path / "archive.tar.gz").exists()


def test_create_compression_rejects_invalid_levels_before_accepting(monkeypatch, quiet_tui, capsys):
    script_prompts(monkeypatch, ["-1", "10", "abc", "9"])
    assert snug._menu_compression(None) == 9
    assert capsys.readouterr().out.count("from 0 to 9") == 3


def test_create_native_review_roundtrip(monkeypatch, tmp_path, quiet_tui):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "Tiếng Việt 🎉.txt"
    source.write_bytes(b"real payload")
    monkeypatch.setattr(snug, "_select_sources_arrow", lambda cwd: [source])
    script_menus(monkeypatch, ["f", choose_format("zip"), "l", "o", "r"])
    script_prompts(monkeypatch, ["0", "output archive.zip"])
    engine = snug.ArchiveEngine(backends=[snug.NativeBackend()])
    snug._menu_create(engine)
    archive = tmp_path / "output archive.zip"
    assert engine.test(archive).bytes_read == len(b"real payload")
    with zipfile.ZipFile(archive) as created:
        assert created.read(source.name) == b"real payload"


def test_members_are_exact_names_and_cancel_keeps_previous_selection(monkeypatch, quiet_tui):
    names = ["a.txt", "a.txt.more", "nested/line\nname.txt"]

    class Engine:
        def inspect(self, archive, password):
            assert password == "secret"
            return ArchiveInspection([snug.ArchiveEntry(name) for name in names], {})

    seen = script_menus(monkeypatch, ["n", "2", "r"])
    assert snug._menu_members(Engine(), Path("a.zip"), "secret", None) == ["a.txt.more"]
    assert "\\x0a" in str(seen[0][1])
    script_menus(monkeypatch, ["a", "b"])
    assert snug._menu_members(Engine(), Path("a.zip"), "secret", [names[0]]) == [names[0]]


def test_extract_forwards_all_options_and_password_to_inspection(monkeypatch, tmp_path, quiet_tui):
    archive = tmp_path / "one.7z"
    monkeypatch.setattr(snug, "_select_archive", lambda: archive)
    secret = "protected value"
    password_file = tmp_path / "password"
    password_file.write_text(secret + "\n")
    script_menus(monkeypatch, ["d", "o", "t", "y", "s", "p", "f", "m", "n", "2", "r",
                               "l", "e", "s", "f", "r", "b", "r"], password=secret)
    script_prompts(monkeypatch, [str(tmp_path / "out"), "1", str(password_file), "2", "20G", "5G", "1000"])
    captured = {}

    class Engine:
        def inspect(self, path, password):
            assert path == archive and password == secret
            return ArchiveInspection([snug.ArchiveEntry("a/one"), snug.ArchiveEntry("a/two")], {})

        def extract(self, path, destination, **kwargs):
            captured.update(path=path, destination=destination, options=kwargs)
            return snug.ExtractReport(path, Path(destination))

    snug._menu_extract(Engine())
    assert captured["path"] == archive
    assert captured["destination"] == str(tmp_path / "out")
    options = captured["options"]
    assert options["members"] == ["a/two"]
    assert options["overwrite"] is False
    assert options["preserve_metadata"] is False
    assert options["symlinks"] == "skip"
    assert options["strip_components"] == 1
    assert options["password"] == secret
    assert options["limits"] == snug.ExtractionLimits(2, 20_000_000_000, 5_000_000_000, 1000)


def test_extract_limits_use_same_units_and_reject_invalid_values(monkeypatch, quiet_tui, capsys):
    script_menus(monkeypatch, ["e", "s", "f", "r", "b"])
    script_prompts(monkeypatch, ["-1", "0", "1kb", "1KiB", "0.5B", "1KB", "nan", "0", "2"])
    assert snug._menu_limits(snug.ExtractionLimits()) == snug.ExtractionLimits(0, 1024, 1000, 2)
    output = capsys.readouterr().out
    assert "nonnegative" in output
    assert "finite positive" in output


def test_extract_limits_can_be_cleared(monkeypatch, quiet_tui):
    script_menus(monkeypatch, ["e", "s", "f", "r", "b"])
    script_prompts(monkeypatch, ["none"] * 4)
    assert snug._menu_limits(snug.ExtractionLimits(1, 2, 3, 4)) == snug.ExtractionLimits()


@pytest.mark.parametrize("cancel", [None, "b"])
def test_extract_cancel_creates_no_destination(monkeypatch, tmp_path, quiet_tui, cancel):
    archive = make_zip(tmp_path / "one.zip")
    destination = tmp_path / "out"
    monkeypatch.setattr(snug, "_select_archive", lambda: archive)
    script_menus(monkeypatch, ["d", cancel])
    script_prompts(monkeypatch, [str(destination)])
    snug._menu_extract(snug.ArchiveEngine())
    assert not destination.exists()


def test_extract_no_members_creates_no_destination(monkeypatch, tmp_path, quiet_tui, capsys):
    archive = make_zip(tmp_path / "one.zip")
    destination = tmp_path / "out"
    monkeypatch.setattr(snug, "_select_archive", lambda: archive)
    script_menus(monkeypatch, ["d", "m", "n", "r", "b", "b"])
    script_prompts(monkeypatch, [str(destination)])
    snug._menu_extract(snug.ArchiveEngine())
    assert "Nothing marked" in capsys.readouterr().out
    assert not destination.exists()


def test_extract_native_selection_no_overwrite_and_strip_are_real(monkeypatch, tmp_path, quiet_tui):
    archive = make_zip(tmp_path / "one.zip")
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / "one.txt").write_bytes(b"old")
    monkeypatch.setattr(snug, "_select_archive", lambda: archive)
    script_menus(monkeypatch, ["d", "m", "n", "1", "r", "o", "t", "y", "s", "r"])
    script_prompts(monkeypatch, [str(destination), "1"])
    snug._menu_extract(snug.ArchiveEngine())
    assert (destination / "one.txt").read_bytes() == b"old"
    assert not (destination / "two.txt").exists()
    assert not list(destination.glob(".snug-part-*"))


def test_extract_member_limit_preserves_old_payload(monkeypatch, tmp_path, quiet_tui):
    archive = make_zip(tmp_path / "one.zip")
    destination = tmp_path / "out"
    (destination / "tree").mkdir(parents=True)
    existing = destination / "tree" / "one.txt"
    existing.write_bytes(b"old")
    monkeypatch.setattr(snug, "_select_archive", lambda: archive)
    script_menus(monkeypatch, ["d", "l", "f", "b", "r"])
    script_prompts(monkeypatch, [str(destination), "2B"])
    with pytest.raises(snug.ResourceLimitError):
        snug._menu_extract(snug.ArchiveEngine())
    assert existing.read_bytes() == b"old"
    assert not list(destination.rglob(".snug-part-*"))


def test_test_action_is_registered_and_reads_real_payload_without_outputs(monkeypatch, tmp_path, quiet_tui, capsys):
    archive = make_zip(tmp_path / "one.zip")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(snug, "_select_archive", lambda: archive)
    script_menus(monkeypatch, ["r"])
    snug._MENU_HANDLERS["5"](snug.ArchiveEngine())
    output = capsys.readouterr().out
    assert "Test archive integrity" in dict(snug._MENU_OPTIONS).values()
    assert "2 entries, 2 files, 9 B decoded" in output
    assert "Archive is OK." in output
    assert list(tmp_path.iterdir()) == [archive]


@pytest.mark.parametrize("cancel", [None, "b"])
def test_test_cancel_does_not_decode(monkeypatch, tmp_path, quiet_tui, cancel):
    archive = tmp_path / "one.zip"
    monkeypatch.setattr(snug, "_select_archive", lambda: archive)
    script_menus(monkeypatch, [cancel])

    class Engine:
        def test(self, *args, **kwargs):
            pytest.fail("cancel decoded archive")

    snug._menu_test(Engine())


def test_password_secure_prompt_and_clear_use_existing_reader(monkeypatch, quiet_tui):
    script_menus(monkeypatch, ["p", "n"])
    calls = []

    def read(args):
        calls.append(args)
        return "secret"

    monkeypatch.setattr(snug, "_read_password", read)
    assert snug._menu_password(None) == "secret"
    assert snug._menu_password("secret") is None
    assert calls[0].password is True and calls[0].password_file is None


@pytest.mark.parametrize("error", [snug.ArchiveError, snug.UnsafeArchiveError, OSError, ValueError])
def test_password_is_redacted_in_operation_errors(monkeypatch, tmp_path, quiet_tui, capsys, error):
    archive = tmp_path / "one.7z"
    secret = "this must never appear"
    monkeypatch.setattr(snug, "_select_archive", lambda: archive)
    monkeypatch.setattr(snug, "_menu_password", lambda current: secret)
    script_menus(monkeypatch, ["p", "r"], password=secret)

    class Engine:
        def test(self, archive, **kwargs):
            assert kwargs["password"] == secret
            raise error("backend rejected " + secret)

    assert snug._run_menu_handler(Engine(), snug._menu_test) is None
    output = capsys.readouterr().out
    assert secret not in output
    assert "[redacted]" in output


def test_encrypted_7z_create_extract_and_test_use_real_password(
        monkeypatch, tmp_path, quiet_tui, py7zr_backend, capsys):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "source.txt"
    source.write_bytes(b"encrypted payload")
    password_file = tmp_path / "password"
    secret = "the real password"
    password_file.write_text(secret + "\n")
    monkeypatch.setattr(snug, "_select_sources_arrow", lambda cwd: [source])
    script_menus(monkeypatch, ["f", choose_format("7z"), "p", "f", "o", "r"], password=secret)
    script_prompts(monkeypatch, [str(password_file), "protected.7z"])
    engine = snug.ArchiveEngine()
    snug._menu_create(engine)
    archive = tmp_path / "protected.7z"
    assert archive.exists()
    monkeypatch.setattr(snug, "_select_archive", lambda: archive)
    script_menus(monkeypatch, ["p", "f", "r"], password=secret)
    script_prompts(monkeypatch, [str(password_file)])
    snug._menu_test(engine)
    destination = tmp_path / "out"
    script_menus(monkeypatch, ["p", "f", "d", "r"], password=secret)
    script_prompts(monkeypatch, [str(password_file), str(destination)])
    snug._menu_extract(engine)
    assert (destination / source.name).read_bytes() == source.read_bytes()
    output = capsys.readouterr().out
    assert "Archive is OK." in output
    assert secret not in output
    assert not list(tmp_path.rglob(".snug-part-*"))


def test_create_changing_writer_clears_unsupported_password(monkeypatch, tmp_path, quiet_tui):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(snug, "_select_sources_arrow", lambda cwd: [tmp_path / "one.txt"])
    monkeypatch.setattr(snug, "_menu_password", lambda current: "secret")
    monkeypatch.setattr(snug, "_interactive_enumerate", lambda *a, **k: ([], 0))
    script_menus(monkeypatch, ["f", "2", "p", "f", "1", "r"], password="secret")

    class Engine:
        def writable_formats(self):
            return [snug.ArchiveFormat.ZIP, snug.ArchiveFormat.SEVEN_ZIP]

        def create(self, archive, sources, **kwargs):
            assert kwargs["password"] is None
            assert kwargs["fmt"] is snug.ArchiveFormat.ZIP
            return snug.CreateReport(archive, kwargs["fmt"])

    snug._menu_create(Engine())


@pytest.mark.parametrize("fmt", [snug.ArchiveFormat.TAR, snug.ArchiveFormat.CPIO, snug.ArchiveFormat.AR])
def test_uncompressed_writers_hide_level_and_clear_previous_setting(monkeypatch, tmp_path, quiet_tui, fmt):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(snug, "_select_sources_arrow", lambda cwd: [tmp_path / "one.txt"])
    monkeypatch.setattr(snug, "_interactive_enumerate", lambda *a, **k: ([], 0))
    seen = script_menus(monkeypatch, ["l", "f", "2", "l", "r"])
    script_prompts(monkeypatch, ["7"])

    class Engine:
        def writable_formats(self):
            return [snug.ArchiveFormat.ZIP, fmt]

        def create(self, archive, sources, **kwargs):
            assert kwargs["compresslevel"] is None
            assert kwargs["fmt"] is fmt
            return snug.CreateReport(archive, fmt)

    snug._menu_create(Engine())
    assert "unavailable" in dict(seen[-1][1])["l"]


@pytest.mark.parametrize("suffix", ["tar", "tar.gz", "gz", "bz2", "xz", "lzma", "ar", "cpio", "rar"])
@pytest.mark.parametrize("handler", [snug._menu_extract, snug._menu_test])
def test_formats_without_password_support_do_not_offer_secure_prompt(
        monkeypatch, tmp_path, quiet_tui, suffix, handler):
    monkeypatch.setattr(snug, "_select_archive", lambda: tmp_path / ("archive." + suffix))
    seen = script_menus(monkeypatch, ["p", "b"])
    monkeypatch.setattr(snug, "_menu_password", lambda current: pytest.fail("unsupported format prompted"))
    handler(snug.ArchiveEngine())
    assert "unavailable" in dict(seen[0][1])["p"]


def test_password_capability_uses_archive_content_for_renamed_zip(tmp_path):
    archive = make_zip(tmp_path / "renamed.bin")
    assert snug._menu_password_supported(archive)


def test_test_real_corruption_reports_failure_without_writing(monkeypatch, tmp_path, quiet_tui, capsys):
    archive = make_zip(tmp_path / "one.zip")
    payload = bytearray(archive.read_bytes())
    with zipfile.ZipFile(archive) as source:
        first = source.infolist()[0]
        offset = first.header_offset + 30 + len(first.filename.encode()) + len(first.extra)
    payload[offset] ^= 1
    archive.write_bytes(payload)
    monkeypatch.setattr(snug, "_select_archive", lambda: archive)
    script_menus(monkeypatch, ["r"])
    assert snug._run_menu_handler(snug.ArchiveEngine(), snug._menu_test) is None
    output = capsys.readouterr().out
    assert "tree/one.txt" in output and "CRC" in output
    assert "Archive is OK." not in output
    assert list(tmp_path.iterdir()) == [archive]


def test_extract_wrong_7z_password_preserves_old_file_and_cleans_staging(
        monkeypatch, tmp_path, quiet_tui, py7zr_backend, capsys):
    source = tmp_path / "source.txt"
    source.write_bytes(b"protected bytes")
    archive = tmp_path / "protected.7z"
    engine = snug.ArchiveEngine()
    engine.create(archive, [source], root=tmp_path, password="correct password")
    destination = tmp_path / "out"
    destination.mkdir()
    old = destination / source.name
    old.write_bytes(b"old")
    wrong = "secret wrong password"
    monkeypatch.setattr(snug, "_select_archive", lambda: archive)
    monkeypatch.setattr(snug, "_menu_password", lambda current: wrong)
    script_menus(monkeypatch, ["p", "d", "r"], password=wrong)
    script_prompts(monkeypatch, [str(destination)])
    assert snug._run_menu_handler(engine, snug._menu_extract) is None
    output = capsys.readouterr().out
    assert "incorrect password or corrupt archive" in output
    assert wrong not in output
    assert old.read_bytes() == b"old"
    assert not list(destination.rglob(".snug-part-*"))
