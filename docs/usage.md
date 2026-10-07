# Usage

[← Back to README](../README.md)

## Interactive mode and command reference

```bash
snug
snug --help
snug --version
snug create --help
snug extract --help
snug list --help
snug info --help
snug test --help
snug doctor --help
snug formats --help
snug update --help
```

With no arguments, Snug opens a terminal menu for creation, extraction, listing, information, and integrity testing. Use arrow keys and Enter to choose an action; `q` exits. The creation picker supports directory navigation, Space to mark sources, and `/` to filter.

The create options screen shows sources, output, installed writable formats, compression, password status, and symlink policy. The extract screen shows destination, exact member selection, overwrite, metadata, symlinks, stripping, password status, and optional limits. Unsupported compression/password settings are marked unavailable. Create, extract, and test use the same engine and secure password inputs as the CLI; password values are never displayed.

Interactive mode requires terminal stdin and stdout. When invoked without arguments in a pipe or redirected session, Snug prints help and exits with status 1. For automation, use the subcommands with their required arguments:

```text
snug create ARCHIVE SOURCE [SOURCE ...] [options]
snug extract ARCHIVE [options]
snug list ARCHIVE [options]
snug info ARCHIVE [options]
snug test ARCHIVE [options]
snug doctor [--json]
snug formats [--json]
snug update [--check | --enable-checks | --disable-checks]
```

Use the full command name. `snug folder/`, `snug archive.zip`, `snug file.txt`, and single-letter aliases are invalid; Snug does not infer an operation from a path. `extract` accepts one archive per command. `create` already accepts multiple files and directories.

## Paths inserted by terminals

Dragging a file into a terminal may insert its quoted or escaped path. Keep the explicit command and pass that path as one argument:

```bash
snug create backup.zip "/Users/lucas/Desktop/My Folder"
snug extract "/Users/lucas/Downloads/My Archive.7z" -C "restored files/"
snug create notes.zip "Tài liệu 📦/" "author's notes.txt" 'double "quotes".txt'
```

Quoting is handled by your shell; Snug receives argument values directly and does not evaluate path strings as shell commands. Spaces, Unicode, Vietnamese text, emoji, quotes, apostrophes, dots, parentheses, and brackets are preserved subject to filesystem limits.

For positional filenames beginning with `-`, use the normal `--` separator. Put options before the separator:

```bash
snug create archive.zip -- -important-file.txt
snug create -q -- -backup.zip -important-file.txt
snug list -- -backup.zip
snug info -- -backup.zip
snug test -- -backup.zip
snug extract -C output/ -- -backup.zip
```

For a leading-hyphen option value, use `=` or an explicit relative path: `snug extract --directory=-output -- -backup.zip` or `snug extract -C ./-output -- -backup.zip`. Quoting a leading hyphen alone does not stop argparse from treating it as an option. All paths after `--` are positional arguments.

## Create archives

```bash
snug create backup.zip folder/
snug create backup.tar.gz folder/ notes.txt
snug create backup.7z folder/ --level 7
snug create backup.bin folder/ --format zip
snug create backup.zip folder/ --force
```

Creation selects the format from the output suffix unless `--format` overrides it. An unknown suffix requires `--format`. Options accepted by the parser include read-only formats, but requesting creation of one fails; see [Supported formats](supported-formats.md).

| Option | Behavior |
|---|---|
| `-f`, `--format FORMAT` | Choose the format explicitly, such as `zip`, `tar.gz`, or `7z`. |
| `-r`, `--root DIR` | Store source paths relative to this directory. Without it, each source starts with its basename. Sources must be within the specified root. |
| `-l`, `--level 0–9` | Set the compression level or preset where supported. Omit for the backend default. |
| `--symlinks store\|follow\|skip` | Store links by default, follow their targets, or omit them. Format limitations still apply. |
| `--force` | Allow replacement of an existing archive. Without it, creation refuses an existing output. |
| `-q`, `--quiet` | Suppress progress and summary output; errors still appear. |

For example, to store `folder/notes.txt` as `notes.txt`:

```bash
snug create notes.zip folder/notes.txt --root folder/
```

ZIP and GZIP use levels 0–9. BZIP2 uses 1–9, with a requested 0 mapped to 1. XZ and LZMA use presets 0–9; 7z uses an LZMA2 preset when a level is supplied. Uncompressed TAR, CPIO, and AR have no compression-level effect. Higher levels generally trade time for compression; the CLI does not select arbitrary codecs.

Snug streams large regular-file payloads and supports ZIP64. Native and libarchive operations report byte progress, speed, and ETA; 7z creation reports item progress because py7zr does not provide input-byte callbacks. Archive creation writes a temporary sibling file and replaces the destination after completion.

## Extract archives

```bash
snug extract backup.zip -C output/
snug extract backup.tar.gz -C output/ --strip-components 1
snug extract backup.7z -C output/ --no-overwrite
snug extract archive.rar -C output/
snug extract image.iso -C image-files/ --symlinks skip
```

The default destination is the current directory. Regular files replace existing file/link targets only after their payloads succeed; a regular-file member cannot replace a conflicting directory. Prefer a new destination or use `--no-overwrite` when existing content must be retained.

Snug extracts regular files into temporary sibling files and commits each completed file atomically where supported. This prevents incomplete regular files from appearing at their final paths. Publication starts after decoding and archive closure succeed. A later commit failure can leave earlier completed files, directories, or links in place; extraction has no whole-operation rollback. See [Security](security.md#temporary-files-and-backend-authority) for filesystem and interruption limits.

| Option | Behavior |
|---|---|
| `-C`, `--directory DIR` | Select the destination directory, creating it if necessary. |
| `-m`, `--member NAME` | Extract an exact member name; repeat to select more. Names match archive paths before stripping. This is not a glob or recursive directory selector. |
| `--strip-components N` | Drop N leading path components; members with no remaining components are omitted. N must be nonnegative. |
| `--no-overwrite` | Skip existing file/link targets and conflicting entries. Existing directories may still receive restored metadata unless `--no-metadata` is also set. |
| `--no-metadata` | Do not restore archived permissions or timestamps. |
| `--symlinks store\|skip` | Create validated links by default, or skip symbolic links. There is no extraction `follow` mode. |
| `--max-files N` | Limit selected archive entries, including directory and link entries. |
| `--max-size SIZE` | Limit total decoded payload bytes. |
| `--max-file-size SIZE` | Limit decoded bytes for each payload member. |
| `--max-ratio RATIO` | Limit decoded/compressed size when reliable compressed sizes are available. |
| `-q`, `--quiet` | Suppress progress and summary output. |

List first to obtain exact member names:

```bash
snug list backup.zip
snug extract backup.zip -C selected/ --member folder/notes.txt --member folder/photo.jpg
```

Extraction detects content signatures before falling back to the suffix. A ZIP renamed to `.bin` can still be read:

```bash
snug create backup.bin folder/ --format zip
snug info backup.bin
snug extract backup.bin -C output/
```

There is no extraction `--format` override. A corrupt header or a recognized suffix does not guarantee that a reader can decode the archive.

### Extraction limits

No limits are enabled by default. Use the full option names; abbreviated extraction options are rejected:

```bash
snug extract backup.zip -C output/ \
  --max-files 100000 --max-size 20G --max-file-size 5G --max-ratio 1000
snug extract backup.zip -C small-output/ --max-size 1M --max-file-size 100K
```

Counts and sizes must be nonnegative; zero permits only entries/payloads within that limit. Ratios must be finite positive numbers. The entry count and known declared sizes are checked before creating the destination, then actual streamed bytes are checked while decoding. Declared-size checks can include selected entries later omitted by stripping, link policy, or `--no-overwrite`; selecting exact members narrows the preflight.

Bare numbers and `B` mean bytes. `K`, `KB`, `M`, `MB`, `G`, `GB`, `T`, and `TB` use powers of 1000; `KiB`, `MiB`, `GiB`, and `TiB` use powers of 1024. For example, `500M` is 500,000,000 bytes, `2G` is 2,000,000,000 bytes, and `1GiB` is 1,073,741,824 bytes. Units are case-sensitive. Fractions are accepted only when they resolve to a whole number of bytes, such as `1.5KiB`; spaces, lowercase/ambiguous units, and fractional bytes are rejected.

Ratio checks are skipped when a member's compressed size is unknown, including solid 7z members and many libarchive formats. Byte limits still apply. These controls do not limit decoder memory, CPU time, or the memory needed to inspect an entry table.

## Standalone compression

Each standalone stream contains exactly one regular-file payload in Snug. Use one source file per command:

```bash
snug create notes.txt.gz notes.txt
snug create notes.txt.bz2 notes.txt
snug create notes.txt.xz notes.txt
snug create notes.txt.lzma notes.txt
snug extract notes.txt.gz -C gzip-output/
snug extract notes.txt.bz2 -C bzip2-output/
snug extract notes.txt.xz -C xz-output/
snug extract notes.txt.lzma -C lzma-output/
```

Each example produces `notes.txt` in its destination. For multiple files or directories, create `tar.gz`, `tar.bz2`, or `tar.xz` instead. See [stream limitations](supported-formats.md#standalone-compression-streams) for naming and inspection behavior.

## List and inspect

```bash
snug list backup.7z
snug list backup.zip --verbose
snug info backup.7z
```

`list` prints member names. `--verbose` adds entry type, permissions, size, modification time, and link target when available. `info` reports the detected format, selected backend, archive size, entry counts, uncompressed size, and current create/extract capabilities. Backends add encryption information only when their APIs establish it. These commands do not guarantee that every data member can be extracted successfully.

## Test archive integrity

```bash
snug test backup.zip
snug test backup.tar.gz
snug test protected.7z --password
snug test notes.txt.xz --quiet
```

`test` consumes real payloads without creating extracted files. ZIP regular-file and symlink payloads are read fully to verify CRCs; TAR regular files and standalone streams are fully decoded; 7z uses a counting null-sink writer; libarchive consumes regular-file blocks. Compressed TAR trailers are also read. Payload transfers are bounded, although codecs retain their own memory requirements.

Success reports entry/file counts, decoded size, and `Archive is OK.` Failures identify the member when available and report the decoder or validation error. `--quiet` suppresses progress and the success summary. Byte progress is used when totals are known, otherwise entry progress is used. Unsafe member structures are rejected before payload testing.

Testing verifies only what the format and decoder can establish. Plain TAR and other formats without payload checksums cannot detect every content change, and a successful test is neither an authenticity check nor a malware scan. `test` does not recursively test archives stored as members.

## Installation diagnostics and capabilities

```bash
snug doctor
snug doctor --json
snug formats
snug formats --json
```

`doctor` is offline and read-only: it performs no update checks, downloads, repairs, or update-state writes. It reports Snug/Python/platform versions, backend availability, py7zr version/streaming API/AES support, libarchive binding/native versions and registered capabilities, runtime state/lock/inventories, and updater installation ownership. Missing optional backends can be healthy for source/pip installs; broken required managed components return status 2. Managed diagnostics need an existing usable Python and do not bootstrap one.

`formats` reports installed read/write capabilities and backend names, including unavailable optional components. It combines backend declarations with successful native reader/filter/writer registrations. Individual codecs, encryption schemes, and archive variants remain conditional; see [Supported formats](supported-formats.md).

Both JSON reports use `schema_version: 1`. Doctor exposes `ok`, `snug_version`, `python`, `platform`, `backends`, `runtime`, and `updater`. Formats exposes `formats` rows with `format`, `read`, `write`, `read_backends`, `write_backends`, `status`, and `details`, plus a capability note. JSON is available only for these diagnostics.

## Passwords

`create`, `extract`, `list`, `info`, and `test` accept either `--password` or `--password-file FILE`; the options are mutually exclusive. `--password` takes no value and prompts securely:

```bash
snug create protected.7z folder/ --password
snug extract protected.7z -C output/ --password
snug list protected.7z --password
snug info protected.7z --password
snug test protected.7z --password
```

Use an existing UTF-8 file containing one password line for noninteractive commands:

```bash
snug extract protected.7z -C output/ --password-file password.txt
```

The final LF or CRLF is removed; spaces are preserved, and embedded line breaks are rejected. Snug refuses a prompt that cannot hide input. It does not print passwords as part of normal output, and optional-backend error messages redact the supplied password. Protect password files yourself; see [Security](security.md#passwords-and-terminal-output).

Only 7z creation supports password protection. Traditional encrypted ZIP can be extracted by the native reader. TAR and standalone streams have no password support, and encrypted RAR extraction is unsupported. See [Supported formats](supported-formats.md) for backend-specific limits.

## Updates

```bash
snug update --check
snug update
snug update --disable-checks
snug update --enable-checks
```

Checks report the latest stable release; only `snug update` requests installation. Supported installer-owned copies update from a verified staged release. Source checkouts, pip installations, and older managed copies receive installation-method guidance. Automatic checks never install a release. See [Updates](updates.md) for network behavior, check interval, state, and failed-update recovery.

## Exit status

| Status | Meaning |
|---|---|
| `0` | Command completed; check summaries for skipped entries. |
| `1` | Interactive mode lacks a terminal, or a managed dependency check/repair failed. |
| `2` | Invalid CLI arguments, an archive/format/I/O error, or unhealthy required components reported by `doctor`. |
| `3` | Extraction or integrity testing rejected an unsafe archive. |
| `4` | An explicit extraction resource limit was exceeded. |
| `130` | Interrupted with Ctrl+C. |

For error messages and fixes, see [Troubleshooting](troubleshooting.md).
