# Architecture

[← Back to README](../README.md)

Snug separates terminal interaction, archive policy, decoding libraries, and managed-runtime setup. The command-line and interactive interfaces call the same `ArchiveEngine`.

```text
snug.py: CLI and terminal interface
└── ArchiveEngine (snug_core.py)
    ├── Native backends: NativeBackend and StreamBackend
    ├── 7z backend: SevenZipBackend (snug_ext.py)
    └── libarchive backend: LibarchiveBackend (snug_ext.py)

Managed launcher: runtime.sh or runtime.ps1
└── snug_runtime.py: check/repair dependencies, then call snug.main
```

## Backend selection and capabilities

[snug_core.py](../snug_core.py) defines the format enum, entry/report models, progress protocol, extraction context, and `ArchiveBackend` protocol. Backends advertise read and write format sets and expose availability and per-file/per-format capability checks.

The default order is native ZIP/TAR, native streams, py7zr, then libarchive. Reading filters available backends by the detected format and asks each candidate whether it can read the file. Writing chooses the first available writer for the requested format. `writable_formats()` combines those checks for the interactive menu and `info`.

The optional-backend module loads third-party libraries lazily. Missing dependencies leave source-installed native formats usable. Managed launchers additionally validate both optional backends before starting any command. Once an operation has selected a reader, decoding errors propagate; there is no automatic retry of partially completed extraction with a second backend.

## Native Python formats

`NativeBackend` uses `zipfile` and `tarfile` for ZIP and TAR containers. It creates Deflate ZIP with ZIP64 support and PAX TAR, optionally compressed with GZIP, BZIP2, or XZ. ZIP readability depends on the compression methods supported by that Python runtime.

`StreamBackend` uses `gzip`, `bz2`, and `lzma` for one-file streams. It derives an output name from the archive basename and scans the decoded data for size information. It does not use untrusted GZIP header names. Regular-file transfers use bounded chunks, normally 1 MiB; entry lists and source enumeration still occupy memory.

## Optional backends

[snug_ext.py](../snug_ext.py) contains both optional adapters:

- `SevenZipBackend` uses py7zr for 7z writing, password support, and reading. Reading prevalidates entries, then supplies a `WriterFactory` whose outputs are owned by Snug. Regular-file writers enforce declared sizes and close completed descriptors. Small symlink payloads are buffered with a 64 KiB limit and created after data writers finish.
- `LibarchiveBackend` uses the `libarchive-c` ctypes binding and native libarchive. It streams decoded entry blocks into Snug's checked extraction functions. It writes CPIO (`cpio_newc`) and AR (`ar_bsd`) only. WARC blocks are trimmed to the declared member size so alignment padding is not written as content.

The format allowlist and writer checks constrain what the library can expose through Snug. See [Supported formats](supported-formats.md) for the public matrix.

## Detection and creation format selection

For reading, detection first checks content: ZIP methods, TAR headers, compression signatures, 7z/RAR signatures, several extended-format headers, and the ISO marker. Compressed GZIP/BZIP2/XZ inputs receive a bounded decoded-header probe to distinguish TAR containers from standalone streams. LZMA-alone has no unique magic, so detection combines a plausible header with a bounded decoder probe.

If the built-in signatures do not identify the file, available backend detection hooks are tried, then known suffixes are used as a fallback. Thin AR is rejected before reading external references. A suffix fallback identifies a candidate format; it does not validate the archive or guarantee codec support.

Creation uses an explicit format, or the output suffix if none is given. It does not inspect existing output content to select the format. Longer suffixes such as `.tar.gz` take priority over `.gz`.

## Creation lifecycle and progress

The engine validates options, selects a writer, enumerates sources with the requested root/link policy, and avoids duplicate archive names and the output archive itself. It writes into a `mkstemp` sibling with a `.part` suffix and replaces the final archive with `os.replace` only when the backend completes. Exceptions and normal interrupts remove the temporary file when possible.

`ProgressSink` provides `start`, `item`, `chunk`, and `done`. `NullProgress` supports callers without presentation; `ProgressDisplay` handles terminal progress, speed, ETA, and plain output for redirected streams. py7zr creation uses item progress because its write API provides no input-byte callbacks. Reports record measured source/output sizes, elapsed time, and skipped entries independently of display.

## Extraction pipeline and metadata

The engine creates and resolves the destination root, builds an `_ExtractContext`, and delegates decoding. Backends select entries and validate names before writes; validation scope differs by backend, with libarchive and py7zr inspecting all entry names and native ZIP/TAR prevalidating selected members. Each filesystem output then passes through path, containment, overwrite, and link checks.

Snug owns directory creation and file/link writes. Libraries do not receive unrestricted disk-extraction authority. The py7zr `extract` call uses Snug's factory rather than the library's normal disk writer, and libarchive yields blocks without a generic extraction call. See [Security](security.md) for the checks and their assumptions.

Permissions and modification times are restored when available and enabled. Permission restoration masks to ordinary `0o777` bits; ownership, ACLs, and extended attributes are not restored. Metadata failures are best effort. Directory metadata is deferred until children are written, then applied deepest first with containment checks. Extraction is incremental, with no transaction that rolls back already written files.

## Managed runtime boundary

[snug_runtime.py](../snug_runtime.py), the platform launchers, and [runtime-lock.json](../runtime-lock.json) belong to the managed installation workflow. They activate application-owned bindings/packages, locate native libraries, probe dependencies, and repair failures before calling the CLI. They are separate from the source/pip entry point. [Runtime management](runtime.md) explains integrity, ownership, and update procedures; [Development](development.md) describes extension points.
