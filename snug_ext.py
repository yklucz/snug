"""Optional archive backends, imported only when the engine needs them.

The libraries decode archive contents; Snug owns every filesystem write.
"""

from __future__ import annotations

import importlib
import io
import os
import stat
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, NoReturn, cast

from snug_core import (
    ArchiveEntry, ArchiveError, ArchiveFormat, CHUNK_SIZE, CreateReport,
    ProgressSink, UnsafeArchiveError, _ExtractContext, _apply_metadata,
    _extract_entry, _prepare_file_target, _prepare_target, _resolve_member,
    _selected, _detect_by_magic, _check_inside,
)


_DLL_DIRECTORIES: dict[str, Any] = {}


def _native_library_search() -> None:
    """Prefer an installed modern libarchive, respecting explicit overrides."""
    if "libarchive" in sys.modules:
        return
    explicit = os.environ.get("LIBARCHIVE")
    if not explicit and sys.platform == "darwin":
        for prefix in ("/opt/homebrew", "/usr/local"):
            candidate = Path(prefix) / "opt/libarchive/lib/libarchive.dylib"
            if candidate.is_file():
                os.environ["LIBARCHIVE"] = str(candidate)
                break
    elif explicit and os.name == "nt" and hasattr(os, "add_dll_directory"):
        directory = str(Path(explicit).expanduser().absolute().parent)
        if Path(directory).is_dir() and directory not in _DLL_DIRECTORIES:
            _DLL_DIRECTORIES[directory] = os.add_dll_directory(directory)


def _formats(*values: str) -> frozenset[ArchiveFormat]:
    return frozenset(ArchiveFormat(value) for value in values)


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return os.fsdecode(value)
    return str(value) if value is not None else ""


def _error_text(exc: Exception, password: str | None = None) -> str:
    message = str(exc)
    if password:
        message = message.replace(password, "[redacted]")
    # Backend errors sometimes include untrusted filenames.
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in message)


class LibarchiveBackend:
    """Broad, streaming extraction through the libarchive-c ctypes binding."""

    name = "libarchive"
    read_formats = _formats(
        "zip", "7z", "rar", "rar5", "zipx", "cab", "iso", "cpio", "ar", "xar",
        "lha", "lzh", "warc", "rpm", "deb",
    )
    write_formats = _formats("cpio", "ar")
    _write_names = {ArchiveFormat("cpio"): "cpio_newc", ArchiveFormat("ar"): "ar_bsd"}

    @staticmethod
    def _library():
        try:
            _native_library_search()
            return importlib.import_module("libarchive")
        except (ImportError, OSError, AttributeError) as exc:
            raise ArchiveError(
                "libarchive support requires libarchive-c and the native libarchive "
                "library (macOS: brew install libarchive; Linux: install libarchive "
                "with your package manager; Windows: provide libarchive.dll)"
            ) from exc

    def available(self) -> bool:
        try:
            self._library()
        except ArchiveError:
            return False
        return True

    def can_read(self, path: Path) -> bool:
        if not self.available():
            return False
        self._validate_signature(path)
        if _detect_by_magic(path) in self.read_formats:
            return True
        return self.detect(path) in self.read_formats

    @staticmethod
    def _validate_signature(path: Path) -> None:
        with path.open("rb") as signature:
            header = signature.read(15)
            if header.startswith(b"!<thin>\n"):
                raise ArchiveError("thin AR archives reference external files and are unsupported")
            # Some native releases accept a bare/truncated RAR5 signature as
            # an empty archive. Even its first generic header requires 15 bytes.
            if header.startswith(b"Rar!\x1a\x07\x01\x00") and len(header) < 15:
                raise ArchiveError("truncated RAR5 archive header")

    def can_write(self, fmt: ArchiveFormat) -> bool:
        if fmt not in self.write_formats or not self.available():
            return False
        library = self._library()
        return self._write_names[fmt] in library.ffi.WRITE_FORMATS

    @staticmethod
    def _reader_format(reader, first_name: str = "", path: Path | None = None):
        name = _text(reader.format_name).lower()
        filters = [_text(value).lower() for value in reader.filter_names]
        if "rpm" in filters:
            return ArchiveFormat("rpm")
        fmt = LibarchiveBackend._named_reader_format(name, first_name)
        if fmt is not None:
            return fmt
        if "zip" in name:
            return ArchiveFormat("zipx" if path and path.suffix.lower() == ".zipx" else "zip")
        if not name and path is not None:
            # Some native releases omit WARC's format_name. A successful
            # reader plus its unambiguous signature still identifies it.
            detected = _detect_by_magic(path)
            if detected == ArchiveFormat("warc"):
                return detected
        return None

    @staticmethod
    def _named_reader_format(name: str, first_name: str) -> ArchiveFormat | None:
        mapping = (
            (("rar5", "rar 5"), "rar5"), (("7-zip", "7zip"), "7z"),
            (("rar",), "rar"), (("cab",), "cab"), (("iso",), "iso"),
            (("cpio",), "cpio"), (("xar",), "xar"), (("lha",), "lha"),
            (("lzh",), "lzh"), (("warc",), "warc"),
        )
        for markers, fmt in mapping:
            if any(marker in name for marker in markers):
                return ArchiveFormat(fmt)
        if name.startswith("ar ") or name in ("ar", "ar (bsd)", "ar (gnu/svr4)"):
            return ArchiveFormat("deb" if first_name == "debian-binary" else "ar")
        return None

    def detect(self, path: Path) -> ArchiveFormat | None:
        if not self.available():
            return None
        library = self._library()
        try:
            self._validate_signature(path)
            with library.file_reader(str(path)) as reader:
                first = next(iter(reader), None)
                return self._reader_format(reader, _text(first.pathname) if first else "", path)
        except (OSError, ValueError, library.exception.ArchiveError, NotImplementedError):
            return None

    detect_format = detect

    @staticmethod
    def _entry(entry) -> ArchiveEntry:
        return ArchiveEntry(
            name=_text(entry.pathname), size=max(0, entry.size or 0),
            is_dir=bool(entry.isdir), is_symlink=bool(entry.issym),
            is_hardlink=bool(entry.islnk),
            link_target=_text(entry.linkpath) if entry.issym or entry.islnk else None,
            mode=entry.mode, mtime=entry.mtime,
        )

    @staticmethod
    def _encryption(library, reader) -> bool | None:
        # The native API reports negative values for unsupported/unknown.
        # Only nonnegative answers are exposed, after scanning the headers.
        try:
            ffi = library.ffi
            probe = getattr(ffi, "read_has_encrypted_entries", None)
            if probe is None:
                probe = ffi.ffi("read_has_encrypted_entries", [ffi.c_archive_p], ffi.c_int)
            result = probe(reader._pointer)
        except AttributeError:
            return None
        return bool(result) if result >= 0 else None

    def _inspect(self, path: Path, password: str | None):
        library = self._library()
        entries: list[ArchiveEntry] = []
        try:
            self._validate_signature(path)
            with library.file_reader(str(path), passphrase=password) as reader:
                first_name = ""
                for raw in reader:
                    entry = self._entry(raw)
                    if not entries:
                        first_name = entry.name
                    entries.append(entry)
                metadata: dict[str, Any] = {}
                fmt = self._reader_format(reader, first_name, path)
                if fmt is not None:
                    metadata["format"] = fmt.value
                encrypted = self._encryption(library, reader)
                if encrypted is not None:
                    metadata["encrypted"] = encrypted
                return entries, metadata
        except ArchiveError:
            raise
        except Exception as exc:
            self._raise_read_error(path, exc, password)

    @staticmethod
    def _raise_read_error(path: Path, exc: Exception, password: str | None) -> NoReturn:
        message = _error_text(exc, password)
        if path.suffix.lower() in (".rar", ".rar5") and any(
            word in message.lower() for word in ("encrypt", "passphrase", "password")
        ):
            raise ArchiveError("encrypted RAR extraction is not supported by the installed libarchive backend") from exc
        raise ArchiveError(f"libarchive could not read {path.name!r}: {message}") from exc

    def list_entries(self, path: Path, password: str | None = None) -> list[ArchiveEntry]:
        return self._inspect(path, password)[0]

    def metadata(self, path: Path, password: str | None = None) -> dict:
        return self._inspect(path, password)[1]

    def extract(self, path: Path, ctx: _ExtractContext, password: str | None = None) -> None:
        library = self._library()
        entries, metadata = self._inspect(path, password)
        for entry in entries:
            _resolve_member(ctx.dest_real, entry.name, ctx.strip)
        if metadata.get("format") in ("rar", "rar5") and metadata.get("encrypted"):
            raise ArchiveError("encrypted RAR extraction is not supported by the installed libarchive backend")
        selected = [entry for entry in entries if _selected(entry.name, ctx.members)]
        ctx.progress.start(sum(e.size for e in selected
                               if not e.is_dir and not e.is_symlink and not e.is_hardlink),
                           len(selected))
        try:
            with library.file_reader(str(path), passphrase=password) as reader:
                for raw in reader:
                    entry = self._entry(raw)
                    if not _selected(entry.name, ctx.members):
                        continue
                    ctx.progress.item(entry.name)
                    if raw.isdir or raw.issym:
                        _extract_entry(ctx, entry)
                    elif raw.islnk:
                        _extract_entry(ctx, entry, hardlink=entry.link_target)
                    elif raw.isreg:
                        _extract_entry(ctx, entry, chunks=self._entry_chunks(raw, raw.size))
                    else:
                        ctx.report.skipped.append(entry.name)
        except ArchiveError:
            raise
        except Exception as exc:
            self._raise_read_error(path, exc, password)

    @staticmethod
    def _entry_chunks(raw, size: int | None) -> Iterable[bytes]:
        # WARC records include alignment padding in libarchive's data stream;
        # only the declared member size belongs in the extracted file.
        remaining = size
        for block in raw.get_blocks(CHUNK_SIZE):
            if remaining is None:
                yield block
            elif remaining > 0:
                chunk = block[:remaining]
                remaining -= len(chunk)
                yield chunk
        if remaining:
            raise ArchiveError(f"truncated archive member: {_text(raw.pathname)!r}")

    @staticmethod
    def _file_chunks(path: Path, progress: ProgressSink) -> Iterable[bytes]:
        with path.open("rb") as source:
            while True:
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    break
                progress.chunk(len(chunk))
                yield chunk

    def create(self, path: Path, items: list[tuple[Path, str]], fmt: ArchiveFormat,
               _compresslevel: int | None, symlinks: str, report: CreateReport,
               progress: ProgressSink, password: str | None = None) -> None:
        library = self._library()
        if not self.can_write(fmt):
            raise ArchiveError(f"{fmt.value} creation is unavailable in the installed libarchive backend")
        if password is not None:
            raise ArchiveError(f"password protection is not supported for {fmt.value} creation")
        if fmt == ArchiveFormat("ar"):
            self._validate_ar_sources(items, symlinks)
        try:
            with library.file_writer(str(path), self._write_names[fmt]) as writer:
                for source, name in items:
                    progress.item(name)
                    self._write_item(writer, source, name, symlinks, report, progress)
        except ArchiveError:
            raise
        except Exception as exc:
            raise ArchiveError(f"libarchive could not create {fmt.value}: {_error_text(exc)}") from exc

    @staticmethod
    def _validate_ar_sources(items: list[tuple[Path, str]], symlinks: str) -> None:
        # Unix ar has a flat regular-file namespace. Never silently flatten.
        for source, name in items:
            if ("/" in name.replace("\\", "/") or not source.is_file()
                    or (source.is_symlink() and symlinks != "follow")):
                raise ArchiveError("AR creation requires regular files with flat archive names; directories and symbolic links are unsupported")

    def _write_item(self, writer, source: Path, name: str, symlinks: str,
                    report: CreateReport, progress: ProgressSink) -> None:
        try:
            st = source.stat() if symlinks == "follow" else source.lstat()
        except (FileNotFoundError, PermissionError):
            report.skipped.append(name)
            return
        attributes = {"permission": stat.S_IMODE(st.st_mode), "mtime": st.st_mtime}
        if stat.S_ISLNK(st.st_mode):
            if symlinks == "skip":
                report.skipped.append(name)
                return
            # libarchive-c's generic linkpath setter can set the hardlink field
            # on a synthetic symlink. Its disk reader preserves real metadata.
            writer.add_files(str(source), pathname=name, recursive=False,
                             symlink_mode="physical")
            report.symlinks += 1
        elif stat.S_ISDIR(st.st_mode):
            writer.add_file_from_memory(name, 0, b"", filetype=stat.S_IFDIR, **attributes)
            report.directories += 1
        elif stat.S_ISREG(st.st_mode):
            writer.add_file_from_memory(name, st.st_size, self._file_chunks(source, progress),
                                        filetype=stat.S_IFREG, **attributes)
            report.files += 1
            report.bytes_in += st.st_size
        else:
            report.skipped.append(name)


class _DiskWriter:
    """Py7zIO-compatible writer owning a safely opened output file."""

    def __init__(self, handle, entry: ArchiveEntry, ctx: _ExtractContext, lock):
        self.handle, self.entry, self.ctx, self.lock = handle, entry, ctx, lock
        self.length = 0
        if entry.size == 0:
            self.handle.close()

    def write(self, data: bytes | bytearray) -> int:
        if self.length + len(data) > self.entry.size:
            raise ArchiveError(f"7z member exceeds its declared size: {self.entry.name!r}")
        count = self.handle.write(data)
        self.length += count
        with self.lock:
            self.ctx.progress.chunk(count)
            self.ctx.report.bytes_written += count
        if self.length == self.entry.size:
            # Release completed member fds even with older factory adapters.
            self.handle.close()
        return count

    def read(self, size: int | None = None) -> bytes:
        if self.handle.closed:
            return b""
        return self.handle.read(-1 if size is None else size)

    def seek(self, offset: int, whence: int = 0) -> int:
        if self.handle.closed and offset == 0 and whence == 0:
            return 0  # MemIO rewinds completed writers before its close hook.
        return self.handle.seek(offset, whence)

    def flush(self) -> None:
        if not self.handle.closed:
            self.handle.flush()

    def close(self) -> None:
        if not self.handle.closed:
            self.handle.close()

    def size(self) -> int:
        return self.length


class _LinkWriter:
    """7z symlink payloads are short UTF-8 paths, never unbounded file data."""

    limit = 65536

    def __init__(self):
        self.buffer = io.BytesIO()

    def write(self, data: bytes | bytearray) -> int:
        if self.buffer.tell() + len(data) > self.limit:
            raise ArchiveError("7z symbolic-link target exceeds the safe 64 KiB limit")
        return self.buffer.write(data)

    def read(self, size: int | None = None) -> bytes:
        return self.buffer.read(-1 if size is None else size)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.buffer.seek(offset, whence)

    def flush(self) -> None:
        # BytesIO has no buffered filesystem writes to flush.
        pass

    def close(self) -> None:
        # Keep the bounded payload available until deferred symlink creation.
        pass

    def size(self) -> int:
        return len(self.buffer.getbuffer())


class _SevenZipWriters:
    """Own checked 7z outputs independently of the optional factory adapter."""

    def __init__(self, library, entries: list[ArchiveEntry], ctx: _ExtractContext):
        self.library = library
        self.ctx = ctx
        self.lock = threading.RLock()
        self.by_name = {entry.name.replace("\\", "/").rstrip("/"): entry
                        for entry in entries if not entry.is_dir}
        self.products: dict[str, Any] = {}

    def create(self, filename: str):
        # Validate py7zr's own normalized output name too. A library
        # rename or an unknown name must never reach the filesystem.
        _resolve_member(self.ctx.dest_real, filename, 0)
        canonical = filename.replace("\\", "/").rstrip("/")
        with self.lock:
            entry = self.by_name.get(canonical)
            if entry is None or canonical in self.products:
                raise UnsafeArchiveError(f"unexpected 7z output path: {filename!r}")
            self.ctx.progress.item(entry.name)
            target = _resolve_member(self.ctx.dest_real, entry.name, self.ctx.strip)
            if target is None:
                raise UnsafeArchiveError(f"empty 7z output path: {entry.name!r}")
            _prepare_target(self.ctx.dest_real, target)
            product = self._product(entry, target)
            self.products[canonical] = (entry, target, product)
            return product

    def _product(self, entry: ArchiveEntry, target: Path):
        exists = target.exists() or target.is_symlink()
        if not self.ctx.overwrite and exists:
            return self._skip(entry)
        if entry.is_symlink:
            return _LinkWriter()
        if not _prepare_file_target(target, self.ctx.overwrite):
            return self._skip(entry)
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        handle = os.fdopen(os.open(target, flags, 0o600), "w+b")
        return _DiskWriter(handle, entry, self.ctx, self.lock)

    def _skip(self, entry: ArchiveEntry):
        self.ctx.report.skipped.append(entry.name)
        return self.library.io.NullIO()

    def finish(self):
        for entry, target, product in self.products.values():
            if isinstance(product, _DiskWriter):
                self._finish_file(entry, target, product)
        # Symlinks are deferred until all data writers are closed, so
        # concurrent decompression cannot follow an archive's new link.
        for entry, target, product in self.products.values():
            if isinstance(product, _LinkWriter):
                try:
                    link_target = product.buffer.getvalue().decode("utf-8")
                except UnicodeError as exc:
                    raise ArchiveError(f"invalid UTF-8 7z symlink target: {entry.name!r}") from exc
                linked_entry = cast(ArchiveEntry, replace(entry, link_target=link_target))
                _extract_entry(self.ctx, linked_entry)

    def _finish_file(self, entry: ArchiveEntry, target: Path, product: _DiskWriter) -> None:
        if product.length != entry.size:
            raise ArchiveError(f"truncated 7z member: {entry.name!r}")
        product.close()
        _check_inside(self.ctx.dest_real, target)
        if target.is_symlink() or not target.is_file():
            raise UnsafeArchiveError(f"7z output changed during extraction: {entry.name!r}")
        _apply_metadata(target, entry.mode, entry.mtime, self.ctx.preserve_metadata)
        self.ctx.report.files += 1

    def close(self):
        for entry, target, product in self.products.values():
            if isinstance(product, _DiskWriter) and not product.handle.closed:
                product.handle.close()


class SevenZipBackend:
    """7z read/write support with a safe streaming writer factory."""

    name = "py7zr"
    read_formats = _formats("7z")
    write_formats = _formats("7z")

    @staticmethod
    def _library():
        try:
            return importlib.import_module("py7zr")
        except (ImportError, OSError) as exc:
            raise ArchiveError("7z support requires py7zr (install Snug with the 7z extra)") from exc

    def available(self) -> bool:
        try:
            self._library()
        except ArchiveError:
            return False
        return True

    def can_read(self, path: Path) -> bool:
        if not self.available():
            return False
        try:
            return bool(self._library().is_7zfile(path))
        except OSError:
            return False

    def can_write(self, fmt: ArchiveFormat) -> bool:
        return fmt in self.write_formats and self.available()

    def detect(self, path: Path) -> ArchiveFormat | None:
        return ArchiveFormat("7z") if self.can_read(path) else None

    detect_format = detect

    @staticmethod
    def _entries(archive) -> list[ArchiveEntry]:
        # ArchiveFile preserves symlink/junction and Unix metadata that FileInfo
        # did not expose in older py7zr releases. No header contents are changed.
        result = []
        for raw in archive.files:
            properties = raw.file_properties()
            timestamp = properties.get("lastwritetime")
            mtime = timestamp.totimestamp() if timestamp is not None else None
            result.append(ArchiveEntry(
                name=raw.filename, size=max(0, raw.uncompressed or 0),
                compressed_size=raw.compressed, is_dir=raw.is_directory,
                is_symlink=raw.is_symlink, mode=properties.get("posix_mode"), mtime=mtime,
            ))
        return result

    @staticmethod
    def _raise_error(exc: Exception, password: str | None) -> NoReturn:
        if isinstance(exc, ArchiveError):
            raise exc
        message = _error_text(exc, password)
        if exc.__class__.__name__ == "PasswordRequired":
            raise ArchiveError("7z archive requires a password; use --password or --password-file") from exc
        if password is not None and exc.__class__.__name__ in ("CrcError", "Bad7zFile", "LZMAError"):
            raise ArchiveError("could not read 7z archive: incorrect password or corrupt archive") from exc
        if password is not None and isinstance(exc, TypeError) and "Unknown field" in message:
            raise ArchiveError("could not read 7z archive: incorrect password or corrupt archive") from exc
        raise ArchiveError(f"py7zr could not process the archive: {message}") from exc

    def list_entries(self, path: Path, password: str | None = None) -> list[ArchiveEntry]:
        library = self._library()
        try:
            with library.SevenZipFile(path, "r", password=password) as archive:
                return self._entries(archive)
        except Exception as exc:
            self._raise_error(exc, password)

    def metadata(self, path: Path, password: str | None = None) -> dict:
        library = self._library()
        try:
            with library.SevenZipFile(path, "r", password=password) as archive:
                return {"format": "7z", "encrypted": bool(archive.needs_password())}
        except Exception as exc:
            self._raise_error(exc, password)

    def create(self, path: Path, items: list[tuple[Path, str]], fmt: ArchiveFormat,
               compresslevel: int | None, symlinks: str, report: CreateReport,
               progress: ProgressSink, password: str | None = None) -> None:
        library = self._library()
        if fmt not in self.write_formats:
            raise ArchiveError(f"py7zr cannot create {fmt.value} archives")
        filters = None
        if compresslevel is not None:
            filters = [{"id": library.FILTER_LZMA2, "preset": compresslevel}]
            if password is not None:
                filters.append({"id": library.FILTER_CRYPTO_AES256_SHA256})
        # py7zr.write does not expose input-byte callbacks. Item progress is
        # honest, while CreateReport still records measured source sizes.
        progress.start(0, len(items))
        try:
            with library.SevenZipFile(path, "w", filters=filters, password=password,
                                      header_encryption=password is not None,
                                      dereference=symlinks == "follow") as archive:
                for source, name in items:
                    progress.item(name)
                    self._write_item(archive, source, name, symlinks, report)
        except Exception as exc:
            self._raise_error(exc, password)

    @staticmethod
    def _write_item(archive, source: Path, name: str, symlinks: str,
                    report: CreateReport) -> None:
        try:
            st = source.stat() if symlinks == "follow" else source.lstat()
        except (FileNotFoundError, PermissionError):
            report.skipped.append(name)
            return
        if stat.S_ISLNK(st.st_mode) and symlinks == "skip":
            report.skipped.append(name)
            return
        if stat.S_ISLNK(st.st_mode) and not source.exists():
            raise ArchiveError(f"7z creation cannot store a dangling symbolic link: {name!r}; use ZIP or TAR")
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode)):
            report.skipped.append(name)
            return
        archive.write(source, arcname=name)
        if stat.S_ISDIR(st.st_mode):
            report.directories += 1
        elif stat.S_ISLNK(st.st_mode):
            report.symlinks += 1
        else:
            report.files += 1
            report.bytes_in += st.st_size

    def extract(self, path: Path, ctx: _ExtractContext, password: str | None = None) -> None:
        library = self._library()
        try:
            with library.SevenZipFile(path, "r", password=password) as archive:
                if archive.needs_password() and password is None:
                    raise ArchiveError("7z archive requires a password; use --password or --password-file")
                entries = self._entries(archive)
                selected = self._validate_entries(archive, entries, ctx)
                ctx.progress.start(sum(e.size for e in selected if not e.is_dir and not e.is_symlink), len(selected))
                factory = self._factory(library, selected, ctx)
                try:
                    for entry in selected:
                        if entry.is_dir:
                            ctx.progress.item(entry.name)
                            _extract_entry(ctx, entry)
                    targets = [entry.name for entry in selected if not entry.is_dir]
                    if targets:
                        archive.extract(targets=targets, recursive=False, factory=factory)
                    factory.finish()
                finally:
                    factory.close()
        except Exception as exc:
            self._raise_error(exc, password)

    @staticmethod
    def _validate_entries(archive, entries: list[ArchiveEntry], ctx: _ExtractContext):
        seen: set[str] = set()
        seen_outputs: set[str] = set()
        selected = []
        for raw, entry in zip(archive.files, entries):
            target = _resolve_member(ctx.dest_real, entry.name, ctx.strip)
            canonical = entry.name.replace("\\", "/").rstrip("/")
            if canonical in seen:
                raise UnsafeArchiveError(f"duplicate 7z member name: {entry.name!r}")
            seen.add(canonical)
            if not SevenZipBackend._supported_entry(raw, entry, ctx):
                continue
            if not _selected(entry.name, ctx.members) or target is None:
                continue
            output_key = os.path.normcase(str(target))
            if output_key in seen_outputs:
                raise UnsafeArchiveError(f"7z members resolve to the same output: {entry.name!r}")
            seen_outputs.add(output_key)
            if entry.is_symlink and ctx.symlinks != "store":
                ctx.report.skipped.append(entry.name)
                continue
            selected.append(entry)
        return selected

    @staticmethod
    def _supported_entry(raw, entry: ArchiveEntry, ctx: _ExtractContext) -> bool:
        if getattr(raw, "is_junction", False):
            raise ArchiveError(f"7z junction extraction is unsupported: {entry.name!r}")
        if getattr(raw, "is_socket", False):
            if _selected(entry.name, ctx.members):
                ctx.report.skipped.append(entry.name)
            return False
        return True

    @staticmethod
    def _factory(library, entries: list[ArchiveEntry], ctx: _ExtractContext):
        writers = _SevenZipWriters(library, entries, ctx)

        class SafeFactory(library.io.WriterFactory):
            def create(self, filename: str):
                return writers.create(filename)

            def finish(self):
                writers.finish()

            def close(self):
                writers.close()

        return SafeFactory()
