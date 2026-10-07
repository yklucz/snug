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

[snug_core.py](../snug_core.py) defines the format enum, inspection/report models, progress protocol, extraction context, and `ArchiveBackend` protocol. Backends advertise read and write format sets and expose availability and per-file/per-format capability checks.

The default order is native ZIP/TAR, native streams, py7zr, then libarchive. Reading filters available backends by the detected format and asks each candidate whether it can read the file. Writing chooses the first available writer for the requested format. `writable_formats()` combines those checks for the interactive menu and `info`.

The optional-backend module loads third-party libraries lazily. Missing dependencies leave source-installed native formats usable. Managed launchers additionally validate both optional backends before archive commands and the terminal TUI. The Unix launcher lets `update` bypass optional-backend checks; the Windows launcher preserves its existing dependency checks and repair before update commands. Offline `doctor`/`formats` use an existing compatible Python without dependency repair on both platforms. Once an operation has selected a reader, decoding errors propagate; there is no automatic retry of partially completed extraction with a second backend.

`inspect(path, password=None)` returns `ArchiveInspection(entries, metadata)`. Engine listing, information, extraction validation, and integrity-test preflight share this model. Compatibility `list_entries()` and `metadata()` methods delegate to inspection. libarchive gathers entries and metadata in one header pass rather than separate scans; extraction reuses that result. Detection and payload decoding can still require their own reads, and no cache is retained across commands.

## Native Python formats

`NativeBackend` uses `zipfile` and `tarfile` for ZIP and TAR containers. It creates Deflate ZIP with ZIP64 support and PAX TAR, optionally compressed with GZIP, BZIP2, or XZ. ZIP readability depends on the compression methods supported by that Python runtime.

`StreamBackend` uses `gzip`, `bz2`, and `lzma` for one-file streams. It derives an output name from the archive basename and does not use untrusted GZIP header names. Listing/information scan decoded data for size; extraction/testing inspect with an unknown decoded size and consume the stream once. Regular-file transfers use bounded chunks, normally 1 MiB; entry lists, source enumeration, and codec state still occupy memory.

## Optional backends

[snug_ext.py](../snug_ext.py) contains both optional adapters:

- `SevenZipBackend` uses py7zr for 7z writing, password support, and reading. Reading prevalidates entries, then supplies a `WriterFactory` backed by the shared staging helper. Regular-file writers enforce declared sizes and close completed descriptors without publishing them. A public file object keeps decoding sequential so interrupted background workers cannot outlive cleanup. Small symlink payloads are buffered with a 64 KiB limit and created after data writers finish.
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

The engine inspects the archive, validates names and selected output collisions, and checks declared resource limits before creating the destination. It resolves the root, builds an `_ExtractContext` carrying the inspection and byte budget, and delegates decoding. Each filesystem output passes through path, containment, overwrite, and link checks.

Snug owns directory creation and file/link writes. Libraries do not receive unrestricted disk-extraction authority. The py7zr `extract` call uses Snug's factory rather than the library's normal disk writer, and libarchive yields blocks without a generic extraction call. See [Security](security.md) for the checks and their assumptions.

`SafeOutputFile` exclusively creates a short random `.snug-part-*` sibling with mode `0o600`, `O_CREAT|O_EXCL`, and `O_NOFOLLOW` when available. It counts decoded bytes, flushes, attempts fsync, closes, and validates known declared sizes. All regular files remain staged until backend decoding and archive closure succeed. The engine then publishes each file with `os.replace`, or exclusive `os.link` for no-overwrite operation, and restores metadata. Unsupported exclusive hard links fail closed; locked destinations can prevent replacement.

The transaction is per regular file. Earlier successful commits, directories, and links are not rolled back after a later failure. Cleanup attempts every staging output without masking an existing failure. Permission restoration masks to ordinary `0o777` bits; ownership, ACLs, and extended attributes are not restored. Metadata failures are best effort. Hardlinks are deferred until regular files are published; directory metadata is applied deepest first with containment checks.

Frozen `ExtractionLimits` holds optional selected-entry, total-byte, per-member-byte, and ratio quotas. `_ExtractionBudget` checks declared sizes, then every streamed write. Unknown compressed sizes skip ratio checks; no compressed size is invented for solid 7z members. Limit failures raise `ResourceLimitError`. No limits are enabled by default, and codec memory/CPU are outside this budget.

## Integrity tests and offline diagnostics

`ArchiveEngine.test()` inspects and validates structure, then invokes the selected backend's payload test. Native ZIP fully reads member payloads for CRC checks. Native TAR consumes regular data and compressed trailers. Standalone streams fully decode. py7zr uses a counting null-sink `WriterFactory`, because packed-stream checks alone do not establish member integrity; libarchive consumes regular-file blocks. These paths write no extracted files and use the shared progress protocol. Format checksums and decoder behavior determine which content corruption can be detected.

`doctor` and `formats` dispatch before `ArchiveEngine` construction or automatic update checks. Doctor performs bounded runtime JSON reads, inventory presence/hash checks, backend API/version/AES checks, and installation-ownership reporting. Native capability probes allocate fresh archive handles, register candidate readers/filters/writers, accept only successful registrations, and free the handles without reading archives. Formats intersects these registrations with backend declarations. Both commands support schema-versioned JSON and perform no downloads, repairs, or state writes.

## Update boundary

[snug_update.py](../snug_update.py) provides shared release metadata checks, semantic version comparison, automatic-check preferences/state, and periodic background checking. The CLI controls when a terminal notice is appropriate. Windows checks and preferences use per-user state; manual installation is managed externally through the PowerShell installer. The macOS/Linux managed replacement implementation remains shared source and does not run on Windows. See [Updates](updates.md).

## Managed runtime boundary

[snug_runtime.py](../snug_runtime.py), the platform launchers, and [runtime-lock.json](../runtime-lock.json) belong to the managed installation workflow. They activate application-owned bindings/packages, locate native libraries, probe dependencies, and repair failures before calling the CLI. They are separate from the source/pip entry point. [Runtime management](runtime.md) explains integrity, ownership, and update procedures; [Development](development.md) describes extension points.
