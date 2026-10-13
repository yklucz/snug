"""TAR hardlinks validate archive order and warn about omitted sources."""

import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

import snug_core as core


SCRIPT = Path(__file__).resolve().parents[1] / "snug.py"


def write_tar(path, entries):
    """Write member order directly, independently of Snug's archive writer."""
    with tarfile.open(path, "w") as archive:
        for name, target in entries:
            member = tarfile.TarInfo(name)
            if target is None:
                member.size = 7
                archive.addfile(member, io.BytesIO(b"payload"))
            else:
                member.type = tarfile.LNKTYPE
                member.linkname = target
                archive.addfile(member)
    return path


def run_cli(*arguments):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, arguments)],
        env=dict(os.environ, SNUG_NO_UPDATE_CHECK="1", PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True, text=True, timeout=10,
    )


@pytest.fixture
def native_engine():
    return core.ArchiveEngine([core.NativeBackend()])


@pytest.mark.parametrize("command", ["test", "extract"])
@pytest.mark.parametrize("entries", [
    [("link", "missing")],
    [("link", "missing"), ("missing", None)],
    [("link", "missing"), ("last", "link")],
])
def test_cli_missing_earlier_hardlink_target_is_corrupt_before_destination(
    tmp_path, command, entries,
):
    # Missing validation would return 0 or publish a forward-reference link.
    archive = write_tar(tmp_path / "dangling.tar", entries)
    destination = tmp_path / "absent"
    arguments = [command, archive]
    if command == "extract":
        arguments += ["-C", destination]
    result = run_cli(*arguments)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert result.stdout == ""
    assert result.stderr.startswith("error: ")
    assert str(archive) in result.stderr
    assert "'link'" in result.stderr and "'missing'" in result.stderr
    assert "earlier" in result.stderr and "complete copy" in result.stderr
    assert "Archive is OK." not in result.stdout + result.stderr
    assert not destination.exists()


def test_subset_still_validates_hardlinks_against_full_archive(native_engine, tmp_path):
    # Excluding a corrupt link must not turn an incomplete archive into a valid one.
    archive = write_tar(tmp_path / "dangling.tar", [("keep", None), ("link", "missing")])
    destination = tmp_path / "absent"
    with pytest.raises(core.ArchiveError, match="missing.*earlier"):
        native_engine.extract(archive, destination, members=["keep"])
    assert not destination.exists()


@pytest.mark.parametrize("command", ["list", "info"])
def test_dangling_hardlink_remains_available_for_diagnosis(tmp_path, command):
    # Moving validation into inspect would reject these diagnostic commands.
    archive = write_tar(tmp_path / "dangling.tar", [("link", "missing")])
    result = run_cli(command, archive)
    assert result.returncode == 0 and result.stderr == ""
    if command == "list":
        assert result.stdout == "link\n"
    else:
        assert result.stdout == (
            f"path              : {archive}\n"
            "format            : tar\nbackend           : native\n"
            "archive_size      : 10.0 KB\nentries           : 1\n"
            "files             : 1\ndirectories       : 0\nsymlinks          : 0\n"
            "uncompressed_size : 0 B\ncan_extract       : yes\n"
            "can_create        : yes\nencrypted         : no\n"
        )


@pytest.mark.parametrize("entries", [
    [("./dir//target", None), ("dir/link", "dir/./target")],
    [("dir\\target", None), ("dir/link", "./dir//target")],
])
def test_hardlink_existence_uses_extractor_name_cleaning(native_engine, tmp_path, entries):
    # Raw archive-name comparison would wrongly reject these equivalent names.
    archive = write_tar(tmp_path / "normalized.tar", entries)
    assert native_engine.test(archive).bytes_read == 7
    destination = tmp_path / "output"
    report = native_engine.extract(archive, destination)
    assert report.files == 2 and not report.skipped
    assert (destination / "dir/link").read_bytes() == b"payload"
    assert os.path.samefile(destination / "dir/target", destination / "dir/link")


@pytest.mark.parametrize("selection", [["link"], lambda name: name == "link"])
def test_unselected_hardlink_source_is_skipped_even_if_destination_exists(
    native_engine, tmp_path, selection,
):
    # A filesystem-only source check would create a link to unrelated existing data.
    archive = write_tar(tmp_path / "subset.tar", [("target", None), ("link", "target")])
    destination = tmp_path / "output"
    destination.mkdir()
    (destination / "target").write_bytes(b"existing target")
    report = native_engine.extract(archive, destination, members=selection)
    assert report.files == 0 and report.skipped == ["link"]
    assert not (destination / "link").exists()
    assert (destination / "target").read_bytes() == b"existing target"
    assert len(report.warnings) == 1
    assert "'link'" in report.warnings[0] and "'target'" in report.warnings[0]
    assert "not selected" in report.warnings[0]


@pytest.mark.parametrize("quiet", [False, True])
def test_cli_unselected_hardlink_has_visible_warning_and_success(tmp_path, quiet):
    # Keeping only the old skipped-name line would hide the omitted dependency.
    archive = write_tar(tmp_path / "subset.tar", [("target", None), ("link", "target")])
    destination = tmp_path / "output"
    arguments = ["extract", archive, "-C", destination, "-m", "link"]
    if quiet:
        arguments.append("-q")
    result = run_cli(*arguments)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "warning:" in result.stderr
    assert "'link'" in result.stderr and "'target'" in result.stderr
    assert "not selected" in result.stderr
    assert not (destination / "link").exists()
    if quiet:
        assert result.stdout == ""


def test_cli_stripped_hardlink_source_has_visible_warning_and_success(tmp_path):
    # Existence must be checked before stripping, while publication uses stripped paths.
    archive = write_tar(tmp_path / "stripped.tar", [("target", None), ("dir/link", "target")])
    destination = tmp_path / "output"
    result = run_cli("extract", archive, "-C", destination, "--strip-components", "1")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "warning:" in result.stderr
    assert "'dir/link'" in result.stderr and "'target'" in result.stderr
    assert "--strip-components" in result.stderr
    assert not (destination / "link").exists()


@pytest.mark.parametrize("strip", [0, 1])
def test_earlier_hardlink_chain_remains_extractable(native_engine, tmp_path, strip):
    # Checking only regular targets would reject a valid chain of earlier links.
    prefix = "dir/" if strip else ""
    archive = write_tar(tmp_path / "chain.tar", [
        (prefix + "target", None),
        (prefix + "middle", prefix + "target"),
        (prefix + "last", prefix + "middle"),
    ])
    assert native_engine.test(archive).bytes_read == 7
    destination = tmp_path / "output"
    report = native_engine.extract(archive, destination, strip_components=strip)
    assert report.files == 3 and not report.skipped
    assert (destination / "last").read_bytes() == b"payload"
    assert os.path.samefile(destination / "target", destination / "last")


def test_selected_hardlink_chain_warns_when_source_was_omitted(native_engine, tmp_path):
    # A later selected chain link must not silently skip an earlier skipped link.
    archive = write_tar(tmp_path / "chain.tar", [
        ("target", None), ("middle", "target"), ("last", "middle"),
    ])
    destination = tmp_path / "output"
    report = native_engine.extract(archive, destination, members=["middle", "last"])
    assert report.files == 0 and report.skipped == ["middle", "last"]
    assert len(report.warnings) == 2
    assert "'middle'" in report.warnings[0] and "'target'" in report.warnings[0]
    assert "'last'" in report.warnings[1] and "'middle'" in report.warnings[1]
    assert not (destination / "last").exists()
