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

Run `snug` without arguments to open the terminal menu. Both stdin and stdout must be terminals. Otherwise Snug prints command help and exits with status 1. [snug.py:3270](../snug.py#L3270), [snug.py:2463](../snug.py#L2463).

Use explicit commands for scripts:

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

Use the full command name. `snug PATH` and single-letter command aliases are invalid. `extract` accepts one archive; `create` accepts one or more sources. [snug.py:2499](../snug.py#L2499), [snug.py:2512](../snug.py#L2512), [snug.py:2528](../snug.py#L2528), [snug.py:2547](../snug.py#L2547), [snug.py:2564](../snug.py#L2564).

### Terminal layout

Full layout needs at least 80 columns and 24 rows. Compact layout needs at least 40 columns and 10 rows. Smaller terminals show a too-small message and accept cancellation. Escape keeps its current back action, including clearing a picker filter first. Ctrl+C keeps the screen's cancellation or quit action. [snug.py:436](../snug.py#L436), [snug.py:105](../snug.py#L105), [snug.py:949](../snug.py#L949).

Compact layout uses shorter hints and scrolls option lists around the selected row. Long text is clipped to the available cells. Result and error pauses show only the rows that fit; they have no scrolling or paging. [snug.py:547](../snug.py#L547), [snug.py:564](../snug.py#L564), [snug.py:451](../snug.py#L451), [snug.py:632](../snug.py#L632).

### Main menu and option menus

The main menu offers creation, extraction, listing, information and integrity testing. Option menus use the same navigation. Single-key shortcuts activate immediately; they do not need Enter. [snug.py:2379](../snug.py#L2379), [snug.py:576](../snug.py#L576).

| Screen | Keys | Action | Code |
|---|---|---|---|
| Main or option menu | Up, Down | Move the selected row. | [snug.py:577](../snug.py#L577) |
| Main or option menu | Enter, shown single-key shortcut | Activate the row or shortcut. | [snug.py:581](../snug.py#L581) |
| Main menu | Escape, q, Q, 0 | Quit with status 0. | [snug.py:584](../snug.py#L584), [snug.py:2435](../snug.py#L2435) |
| Main menu | Ctrl+C | Quit with status 130. | [snug.py:2433](../snug.py#L2433) |
| Option menu | Escape, q, Q, Ctrl+C | Leave the menu. A shown b shortcut goes Back. | [snug.py:130](../snug.py#L130), [snug.py:584](../snug.py#L584), [snug.py:1828](../snug.py#L1828), [snug.py:2275](../snug.py#L2275) |

Creation options show sources, output, installed writers, compression, password status and symlink policy. Extraction options show destination, members, overwrite, metadata, symlinks, stripping, password status and limits. Unsupported settings are marked unavailable. Password labels show only whether a password is set. [snug.py:1748](../snug.py#L1748), [snug.py:2201](../snug.py#L2201), [snug.py:1723](../snug.py#L1723).

The TUI calls the same archive engine as the CLI. [snug.py:1896](../snug.py#L1896), [snug.py:2302](../snug.py#L2302), [snug.py:2332](../snug.py#L2332), [snug.py:2674](../snug.py#L2674), [snug.py:2693](../snug.py#L2693).

### Source picker and archive browser

Creation opens a multi-select source picker. The archive path prompt opens a single-file browser with Tab. Cancelling that browser returns to the path prompt with its draft preserved. Choosing a file still checks that it is a file before continuing. [snug.py:1856](../snug.py#L1856), [snug.py:1570](../snug.py#L1570), [snug.py:1582](../snug.py#L1582), [snug.py:1589](../snug.py#L1589).

| Screen | Keys | Action | Code |
|---|---|---|---|
| Either picker | Up, Down | Move through visible entries. | [snug.py:966](../snug.py#L966) |
| Either picker | Enter, Right | Open the highlighted directory. Enter retries a failed directory read. | [snug.py:925](../snug.py#L925), [snug.py:994](../snug.py#L994) |
| Either picker | Left | Go to the parent. The source picker stops at its starting directory; the archive browser can go above it. | [snug.py:1014](../snug.py#L1014) |
| Source picker | Space | Toggle a mark on the highlighted entry. | [snug.py:962](../snug.py#L962), [snug.py:1029](../snug.py#L1029) |
| Source picker | Tab | Accept the marked set. Stay if nothing is marked. | [snug.py:923](../snug.py#L923), [snug.py:1053](../snug.py#L1053) |
| Source picker | Enter on a file | Accept the marked set if there are marks; otherwise accept the highlighted file. | [snug.py:934](../snug.py#L934), [snug.py:1053](../snug.py#L1053) |
| Archive browser | Enter on a file, Tab | Choose one highlighted file. Space does not mark entries. | [snug.py:1041](../snug.py#L1041), [snug.py:962](../snug.py#L962) |
| Either picker | /, printable text | Start a name filter. q and Q cancel when no filter is active; while filtering they are text. | [snug.py:941](../snug.py#L941), [snug.py:976](../snug.py#L976) |
| Either picker | Backspace | Remove the last filter character. | [snug.py:957](../snug.py#L957) |
| Either picker | Escape | Clear active filter text or empty filter mode first. A second Escape cancels. | [snug.py:949](../snug.py#L949) |
| Either picker | Ctrl+C | Cancel immediately, even while filtering. | [snug.py:1080](../snug.py#L1080) |

Marking a directory marks only that source entry, without marking each child. Creation still walks the selected directory. Directory symlinks listed in the picker are not opened. [snug.py:1029](../snug.py#L1029), [snug_core.py:924](../snug_core.py#L924), [snug.py:674](../snug.py#L674), [snug.py:1007](../snug.py#L1007).

Filters compare names and filter text in NFC form, ignoring case. Selected paths keep their original spelling. [snug.py:795](../snug.py#L795), [snug.py:1033](../snug.py#L1033), [snug.py:1051](../snug.py#L1051).

### Archive members

Members are exact archive names, as with CLI `--member`. Selecting a directory member does not select its descendants. [snug.py:1913](../snug.py#L1913), [snug.py:2302](../snug.py#L2302), [snug.py:2696](../snug.py#L2696), [snug_core.py:779](../snug_core.py#L779).

| Keys | Action | Code |
|---|---|---|
| Up, Down | Move between member and action rows. | [snug.py:1987](../snug.py#L1987) |
| Space | Toggle the highlighted member; action rows are unchanged. | [snug.py:1996](../snug.py#L1996) |
| Enter, shown single-key shortcut | Activate that member or action row. | [snug.py:1994](../snug.py#L1994), [snug.py:1999](../snug.py#L1999) |
| a, n | Mark all members, or clear all marks. | [snug.py:1977](../snug.py#L1977) |
| Tab, r | Use the selection. An empty selection stays on this screen. | [snug.py:1992](../snug.py#L1992), [snug.py:1971](../snug.py#L1971) |
| Escape, b, q, Q, Ctrl+C | Discard changes and return to extraction options. | [snug.py:130](../snug.py#L130), [snug.py:1964](../snug.py#L1964), [snug.py:1990](../snug.py#L1990) |

### Extraction limits

Limits apply to the current extraction review. Returning from the limits screen with Apply keeps those edits in that review. A new extraction starts with no limits enabled. [snug.py:2262](../snug.py#L2262), [snug.py:2294](../snug.py#L2294), [snug.py:2199](../snug.py#L2199), [snug_core.py:195](../snug_core.py#L195).

| Screen | Keys | Action | Code |
|---|---|---|---|
| Limits overview | Up, Down, Enter | Move and activate a row. | [snug.py:2136](../snug.py#L2136) |
| Limits overview | e, s, f, r | Edit entry count, total bytes, bytes per member, or compression ratio. | [snug.py:2045](../snug.py#L2045), [snug.py:2147](../snug.py#L2147) |
| Limits overview | Tab, a | Apply edits and return to extraction options. | [snug.py:2144](../snug.py#L2144) |
| Limits overview | Escape, b, q, Q, Ctrl+C | Discard edits and return to extraction options. | [snug.py:130](../snug.py#L130), [snug.py:2137](../snug.py#L2137) |
| Limit field | Printable text, Backspace | Append text or erase the last character. | [snug.py:2125](../snug.py#L2125) |
| Limit field | Enter | Save a valid field value to the overview. Empty keeps the value; none clears it. Apply is still needed. | [snug.py:2108](../snug.py#L2108), [snug.py:2144](../snug.py#L2144) |
| Limit field | Escape, Ctrl+C | Discard only this field draft and return to the overview. | [snug.py:130](../snug.py#L130), [snug.py:2103](../snug.py#L2103) |

Size fields accept decimal K/KB and binary KiB units. Limit fields use a small append-and-backspace editor, rather than the line editor below. While editing a field, b, q and Q are text, not discard shortcuts. [snug_core.py:210](../snug_core.py#L210), [snug.py:2103](../snug.py#L2103), [snug.py:2132](../snug.py#L2132).

### Line prompts and history

TTY line prompts use Snug's editor. Escape and Ctrl+C cancel the current prompt. Ctrl+D on an empty line ends the session with status 0. Cancelling a setting keeps its previous value. Rejected archive paths are prefilled for editing; spaces in that path are preserved. [snug.py:1266](../snug.py#L1266), [snug.py:1466](../snug.py#L1466), [snug.py:2403](../snug.py#L2403), [snug.py:1777](../snug.py#L1777), [snug.py:1570](../snug.py#L1570), [snug.py:1589](../snug.py#L1589).

| Keys | Action | Code |
|---|---|---|
| Printable text, Space | Insert at the cursor. | [snug.py:1317](../snug.py#L1317) |
| Left, Right | Move the cursor. | [snug.py:1280](../snug.py#L1280) |
| Home, Ctrl+A; End, Ctrl+E | Move to the start or end. | [snug.py:1276](../snug.py#L1276) |
| Backspace | Delete before the cursor. | [snug.py:1284](../snug.py#L1284) |
| Delete, Ctrl+D on a nonempty line | Delete at the cursor. | [snug.py:1287](../snug.py#L1287) |
| Ctrl+K | Delete from the cursor to the end. | [snug.py:1289](../snug.py#L1289) |
| Ctrl+U | Delete before the cursor. | [snug.py:1291](../snug.py#L1291) |
| Ctrl+W | Delete the preceding whitespace and word. | [snug.py:1294](../snug.py#L1294) |
| Up, Down | Recall history for this prompt kind; Down can restore the current draft. | [snug.py:1303](../snug.py#L1303) |
| Enter | Submit the line. | [snug.py:1267](../snug.py#L1267) |
| Escape, Ctrl+C | Cancel the prompt. | [snug.py:1267](../snug.py#L1267), [snug.py:1470](../snug.py#L1470) |
| Ctrl+D on an empty line | End the session with status 0. | [snug.py:1269](../snug.py#L1269), [snug.py:1466](../snug.py#L1466), [snug.py:2403](../snug.py#L2403) |
| Tab in the archive path prompt | Browse for one file. | [snug.py:1572](../snug.py#L1572) |

Submitted nonempty lines are kept in memory by prompt kind for this process. Prompt history is not written to disk. TTY prompts do not use readline history, Ctrl+R reverse search, Ctrl+V quoted insertion, ~/.inputrc bindings, or vi mode. [snug.py:1396](../snug.py#L1396), [snug.py:1402](../snug.py#L1402), [snug.py:1437](../snug.py#L1437), [snug.py:1461](../snug.py#L1461), [snug.py:312](../snug.py#L312), [snug.py:384](../snug.py#L384).

### Results, errors and interrupts

| Keys | Action | Code |
|---|---|---|
| Enter, Escape, Ctrl+C | Dismiss a result or error pause and return to the main menu. | [snug.py:626](../snug.py#L626), [snug.py:646](../snug.py#L646), [snug.py:130](../snug.py#L130), [snug.py:2418](../snug.py#L2418) |

Ctrl+C acts like Escape in option menus, members, limits and line prompts. In either picker it cancels immediately, while Escape clears filtering first. At the main menu Ctrl+C quits with status 130. [snug.py:130](../snug.py#L130), [snug.py:1470](../snug.py#L1470), [snug.py:949](../snug.py#L949), [snug.py:1080](../snug.py#L1080), [snug.py:2433](../snug.py#L2433).

Passwords use the CLI's secure getpass prompt, not this line editor. Ctrl+C at that prompt, or during an archive engine operation, can end the session with status 130. Source enumeration before creation can be cancelled with Escape, q, Q or Ctrl+C. [snug.py:1636](../snug.py#L1636), [snug.py:2580](../snug.py#L2580), [snug.py:2459](../snug.py#L2459), [snug.py:1196](../snug.py#L1196).

Extraction is transactional per regular file. Files are staged and published after decoding succeeds. A later publication failure can leave earlier files, directories or links in place. There is no whole-archive rollback. [snug_core.py:1153](../snug_core.py#L1153), [snug_core.py:2135](../snug_core.py#L2135).

### Non-TTY fallback

The no-argument entry point does not start a session when either stream is not a terminal. Helpers reached without both TTY streams use numbered text menus and ordinary line input; arrow picking and pause screens are bypassed. Enter the displayed option and press Enter. q, quit or exit cancels a numbered menu. [snug.py:2463](../snug.py#L2463), [snug.py:593](../snug.py#L593), [snug.py:615](../snug.py#L615), [snug.py:1464](../snug.py#L1464), [snug.py:1075](../snug.py#L1075), [snug.py:661](../snug.py#L661).

In the limits fallback, b still applies the edited limits. EOF or Ctrl+C at a fallback line prompt ends the session with status 0. EOF in a numbered menu cancels that menu; at the main menu this ends the session with status 0. EOF at the limits overview keeps the original limits. [snug.py:2176](../snug.py#L2176), [snug.py:2174](../snug.py#L2174), [snug.py:2403](../snug.py#L2403), [snug.py:1466](../snug.py#L1466), [snug.py:1470](../snug.py#L1470), [snug.py:601](../snug.py#L601), [snug.py:2435](../snug.py#L2435).

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
| `130` | Command interrupted. In the TUI, main-menu, secure-password or archive-operation interruptions can exit with 130; screen cancellation follows the rules above. [snug.py:2433](../snug.py#L2433), [snug.py:2459](../snug.py#L2459), [snug.py:3261](../snug.py#L3261). |

For error messages and fixes, see [Troubleshooting](troubleshooting.md).
