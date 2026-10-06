from pathlib import Path
import sys

import pytest

# The checkout is deliberately still importable as a standalone script project.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import snug


@pytest.fixture
def engine():
    return snug.ArchiveEngine()


@pytest.fixture
def source_tree(tmp_path):
    tree = tmp_path / "source"
    (tree / "nested").mkdir(parents=True)
    (tree / "empty_dir").mkdir()
    (tree / "hello.txt").write_bytes(b"hello Snug\n")
    (tree / "nested" / "binary.bin").write_bytes(bytes(range(256)) * 4096)
    (tree / "empty.txt").touch()
    return tree


@pytest.fixture
def py7zr_backend():
    return pytest.importorskip("py7zr", reason="optional py7zr backend unavailable")


@pytest.fixture
def libarchive_backend():
    try:
        import libarchive
    except (ImportError, OSError) as exc:
        pytest.skip(f"optional libarchive backend unavailable: {exc}")
    return libarchive


@pytest.fixture
def fixture_dir():
    return Path(__file__).parent / "fixtures"
