from pathlib import Path

import pytest

import snug
import snug_ext


PASSWORDS = ("canary\\path\t'\"", "canaryé\\path\t'\"")
REPRESENTATIONS = (str, repr, ascii)


@pytest.mark.parametrize("password", PASSWORDS)
@pytest.mark.parametrize("representation", REPRESENTATIONS)
def test_backend_error_redacts_password_representations(password, representation):
    message = f"backend rejected {representation(password)}"
    shown = snug_ext._error_text(RuntimeError(message), password)
    assert "canary" not in shown
    assert "[redacted]" in shown


@pytest.mark.parametrize("password", PASSWORDS)
@pytest.mark.parametrize("representation", REPRESENTATIONS)
@pytest.mark.parametrize("error_type", [snug.ArchiveError, snug.UnsafeArchiveError, OSError, ValueError])
def test_tui_guard_redacts_password_representations(password, representation, error_type):
    error = error_type(f"backend rejected {representation(password)}")
    with pytest.raises(snug.ArchiveError) as caught:
        with snug._menu_password_errors(password):
            raise error
    assert "canary" not in str(caught.value)
    assert "[redacted]" in str(caught.value)
    if isinstance(error, snug.ArchiveError):
        assert type(caught.value) is error_type
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("password", PASSWORDS)
@pytest.mark.parametrize("representation", [repr, ascii])
@pytest.mark.parametrize("backend", ["py7zr", "libarchive"])
def test_backend_repr_error_reaches_tui_without_password(password, representation, backend, capsys):
    error = RuntimeError(f"backend rejected {representation(password)}")

    def operation(_engine):
        with snug._menu_password_errors(password):
            if backend == "py7zr":
                snug_ext.SevenZipBackend._raise_error(error, password)
            snug_ext.LibarchiveBackend._raise_read_error(Path("archive.zip"), error, password)

    assert snug._run_menu_handler(None, operation) is None
    output = capsys.readouterr().out
    assert "canary" not in output
    assert "[redacted]" in output
    assert "error:" in output


@pytest.mark.parametrize("password", [None, "", "unrelated"])
def test_unrelated_error_text_is_preserved(password):
    assert snug_ext._error_text(RuntimeError("ordinary\nmessage"), password) == "ordinary\\nmessage"
    error = snug.ArchiveError("ordinary message")
    with pytest.raises(snug.ArchiveError) as caught:
        with snug._menu_password_errors(password):
            raise error
    assert caught.value is error


def test_duplicate_password_representations_are_replaced_once():
    assert snug_ext._error_text(RuntimeError("red"), "red") == "[redacted]"
    with pytest.raises(snug.ArchiveError, match=r"^\[redacted\]$"):
        with snug._menu_password_errors("red"):
            raise snug.ArchiveError("red")
