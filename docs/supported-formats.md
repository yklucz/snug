# Supported formats

[← Back to README](../README.md)

The table describes implemented capabilities with the appropriate dependencies installed. It does not guarantee every codec, encrypted variant, or multi-volume form of a format. See [Installation](installation.md) for backend setup and [Usage](usage.md) for commands.

| Format | Extract | Create | Backend |
|---|:---:|:---:|---|
| ZIP | ✅ | ✅ | Python standard library; libarchive fallback for reading |
| TAR variants | ✅ | ✅ | Python standard library |
| GZIP / BZIP2 / XZ / LZMA | ✅ | ✅ | Python standard library |
| 7z | ✅ | ✅ | py7zr; libarchive fallback for extraction only |
| RAR / RAR5 | ✅ | ❌ | libarchive |
| ZIPX | ✅ | ❌ | libarchive; native reading when its ZIP methods suffice |
| CAB / ISO | ✅ | ❌ | libarchive |
| CPIO | ✅ | ✅ | libarchive |
| AR | ✅ | ✅ | libarchive |
| XAR | ✅ | ❌ | libarchive |
| LHA / LZH | ✅ | ❌ | libarchive |
| WARC | ✅ | ❌ | libarchive |
| RPM / DEB | ✅ | ❌ | libarchive |

TAR creation supports `tar`, `tar.gz`, `tar.bz2`, and `tar.xz` (including suffix aliases `.tgz`, `.tbz`, `.tbz2`, and `.txz`). There is no `tar.lzma` creation format. Standalone LZMA uses the LZMA-alone container.

CPIO creation uses `cpio_newc`; AR creation uses `ar_bsd`. Their availability depends on the native library advertising those writers. Use `snug info archive` to see the selected backend and whether its detected format can be created with the installed backends. Listing or inspecting an archive is not a full integrity test of every payload.

## ZIP and ZIPX

ZIP creation uses Deflate, streaming file I/O, and ZIP64 when needed. Native reading supports stored, Deflate, BZIP2, and LZMA methods; Zstandard reading is available when the Python runtime exposes it. Unsupported native ZIP methods are classified as ZIPX and routed to libarchive when available, regardless of the filename suffix.

ZIPX support depends on the codecs enabled in the installed libarchive build. The local fixture suite includes a PPMd ZIPX archive; that does not establish support for every ZIPX codec. A `.zipx` file using only native methods may use the standard-library reader, but Snug does not offer ZIPX creation.

Traditional password-protected ZIP extraction is covered by a local fixture. Native ZIP creation cannot encrypt archives. Other ZIP encryption schemes depend on the selected reader; do not assume AES ZIP support from traditional ZIP coverage.

## 7z and links

py7zr provides 7z creation and extraction, including password protection and encrypted headers on creation. libarchive can read 7z when py7zr is unavailable; it cannot create 7z through Snug. Once py7zr is selected, a decoding failure does not automatically retry extraction with another backend.

The py7zr extraction path rejects junction entries and duplicate or colliding output names. Safe relative symbolic links can be stored and extracted, subject to platform privileges. py7zr creation rejects dangling symlinks; use ZIP or TAR to store them, or choose `--symlinks skip`. Extraction may create an in-destination dangling link if its target passes containment checks; this does not imply that every backend can create or read such links.

Windows symlink creation may require Developer Mode or an elevated process, depending on the host configuration. Use `--symlinks skip` for extraction when links are unnecessary.

## RAR and multi-volume archives

RAR and RAR5 extraction use libarchive. Snug cannot create RAR. Encrypted RAR extraction is explicitly rejected when detected or reported by the backend; supplying a password does not add support.

There is no CLI for splitting archives or assembling multi-volume sets. Snug opens a single archive path and does not orchestrate adjacent RAR, 7z, or ZIP volumes. Multi-volume archives are not a supported workflow, even though `multivolumefile` is a py7zr dependency.

## AR, RPM, and DEB

- AR creation requires regular files with flat archive names. Directories, nested names, and stored symlinks are rejected. Thin AR is rejected because it references files outside the archive.
- RPM extraction reads the wrapped CPIO payload through libarchive. It does not install packages, run package scripts, or preserve a complete package-manager database.
- DEB extraction reads the outer AR container: typically `debian-binary`, `control.tar.*`, and `data.tar.*`. It does not recursively unpack the nested TAR archives or install the package. Extract a nested archive with a separate command if needed.

## Standalone compression streams

`.gz`, `.bz2`, `.xz`, and `.lzma` expose exactly one regular-file output in Snug. Creation accepts exactly one regular-file source; directories, multiple sources, and stored symlinks are rejected. A symlink to a regular file can be read with `--symlinks follow`. Use a TAR variant for a directory or multiple files.

The extracted name comes from the input basename with the compression suffix removed, or gains `.out` when no known suffix exists. Snug ignores the GZIP header filename. These streams do not support passwords or archive-level file metadata. `list` and `info` decompress the stream to calculate its uncompressed size.

## Other boundaries and evidence

Snug does not advertise MSI, NSIS, arbitrary EXE installers, or obscure legacy formats outside the matrix. A library's broader format support does not automatically become a Snug capability. Renaming an unsupported format does not convert it.

The tests exercise native round trips, 7z passwords and links, CPIO/AR creation, a DEB container, thin AR rejection, and independent RAR/RAR5, CAB, ISO, ZIPX, LHA/LZH, WARC, RPM, XAR, and 7z fixtures. See [Development](development.md) and the [fixture provenance](../tests/fixtures/README.md) for test setup and evidence. Reader behavior can differ across native library versions and operating systems. The [security model](security.md) applies to extraction regardless of format.
