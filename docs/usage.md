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

Run `snug` without arguments to open the terminal menu. Both stdin and stdout must be terminals. Otherwise Snug prints command help and exits with status 1. [snug.py:4143](../snug.py#L4143), [snug.py:2864](../snug.py#L2864).

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

Use the full command name. `snug PATH` and single-letter command aliases are invalid. `extract` accepts one archive; `create` accepts one or more sources. [snug.py:2900](../snug.py#L2900), [snug.py:2913](../snug.py#L2913), [snug.py:2929](../snug.py#L2929), [snug.py:2948](../snug.py#L2948), [snug.py:2965](../snug.py#L2965).

### Terminal layout

Full layout needs at least 80 columns and 24 rows. Compact layout needs at least 40 columns and 10 rows. Smaller terminals show a too-small message and accept cancellation. Escape keeps its current back action, including clearing a picker filter first. Ctrl+C keeps the screen's cancellation or quit action. [snug.py:465](../snug.py#L465), [snug.py:125](../snug.py#L125), [snug.py:1101](../snug.py#L1101), [snug.py:114](../snug.py#L114).

Compact layout uses shorter hints and scrolls option lists around the selected row. It keeps action rows visible. Menu text is clipped to the available cells. [snug.py:541](../snug.py#L541), [snug.py:576](../snug.py#L576), [snug.py:593](../snug.py#L593), [snug.py:480](../snug.py#L480).

Result and error pauses wrap messages and shorten long paths in the middle. Up, Down, Page Up, Page Down, Home and End scroll the details. An overflow row shows the hidden line count and visible range. The key footer stays on screen. [snug.py:732](../snug.py#L732), [snug.py:753](../snug.py#L753), [snug.py:769](../snug.py#L769), [snug.py:777](../snug.py#L777).

### Main menu and option menus

The main menu offers creation, extraction, listing, information and integrity testing. Option menus use the same navigation. Single-key shortcuts activate immediately; they do not need Enter. [snug.py:2780](../snug.py#L2780), [snug.py:605](../snug.py#L605).

| Screen | Keys | Action | Code |
|---|---|---|---|
| Main or option menu | Up, Down | Move the selected row. | [snug.py:606](../snug.py#L606) |
| Main or option menu | Enter, shown single-key shortcut | Activate the row or shortcut. | [snug.py:610](../snug.py#L610) |
| Main menu | Escape, q, Q, 0 | Quit with status 0. | [snug.py:613](../snug.py#L613), [snug.py:2836](../snug.py#L2836) |
| Main menu | Ctrl+C | Quit with status 130. | [snug.py:2834](../snug.py#L2834) |
| Option menu | Escape, q, Q, Ctrl+C | Leave the menu. A shown b shortcut goes Back. | [snug.py:114](../snug.py#L114), [snug.py:613](../snug.py#L613), [snug.py:2061](../snug.py#L2061), [snug.py:2676](../snug.py#L2676) |

Creation options show sources, output, installed writers, compression, password status and symlink policy. Extraction options show destination, members, overwrite, metadata, symlinks, stripping, password status and limits. Unsupported settings are marked unavailable. Password labels show only whether a password is set. [snug.py:1981](../snug.py#L1981), [snug.py:2602](../snug.py#L2602), [snug.py:1956](../snug.py#L1956).

The TUI calls the same archive engine as the CLI. [snug.py:2243](../snug.py#L2243), [snug.py:2701](../snug.py#L2701), [snug.py:2731](../snug.py#L2731), [snug.py:3510](../snug.py#L3510), [snug.py:3543](../snug.py#L3543).

### Source picker and archive browser

Creation opens a multi-select source picker. The archive path prompt opens a single-file browser with Tab. Cancelling that browser returns to the path prompt with its draft preserved. Choosing a file still checks that it is a file before continuing. [snug.py:2253](../snug.py#L2253), [snug.py:1788](../snug.py#L1788), [snug.py:1811](../snug.py#L1811), [snug.py:1818](../snug.py#L1818).

| Screen | Keys | Action | Code |
|---|---|---|---|
| Either picker | Up, Down | Move through visible entries. | [snug.py:1121](../snug.py#L1121) |
| Either picker | Enter, Right | Open the highlighted directory. Enter retries a failed directory read. | [snug.py:1080](../snug.py#L1080), [snug.py:1149](../snug.py#L1149) |
| Either picker | Left | Go to the parent. The source picker stops at its starting directory; the archive browser can go above it. | [snug.py:1169](../snug.py#L1169) |
| Source picker | Space | Toggle a mark on the highlighted entry. | [snug.py:1117](../snug.py#L1117), [snug.py:1184](../snug.py#L1184) |
| Source picker | Tab | Accept the marked set. Stay if nothing is marked. | [snug.py:1078](../snug.py#L1078), [snug.py:1203](../snug.py#L1203) |
| Source picker | Enter on a file | Accept the marked set if there are marks; otherwise accept the highlighted file. | [snug.py:1089](../snug.py#L1089), [snug.py:1203](../snug.py#L1203) |
| Archive browser | Enter on a file, Tab | Choose one highlighted file. Space does not mark entries. | [snug.py:1196](../snug.py#L1196), [snug.py:1117](../snug.py#L1117) |
| Either picker | /, printable text | Start a name filter. q and Q cancel when no filter is active; while filtering they are text. | [snug.py:1096](../snug.py#L1096), [snug.py:1131](../snug.py#L1131) |
| Either picker | Backspace | Remove the last filter character. | [snug.py:1112](../snug.py#L1112) |
| Either picker | Escape | Clear active filter text or empty filter mode first. A second Escape cancels. | [snug.py:1104](../snug.py#L1104) |
| Either picker | Ctrl+C | Cancel immediately, even while filtering. | [snug.py:1244](../snug.py#L1244) |

Marking a directory marks only that source entry, without marking each child. Creation still walks the selected directory. Directory symlinks listed in the picker are not opened. [snug.py:1184](../snug.py#L1184), [snug_core.py:957](../snug_core.py#L957), [snug.py:827](../snug.py#L827), [snug.py:1162](../snug.py#L1162).

Filters compare names and filter text in NFC form, ignoring case. Selected paths keep their original spelling. [snug.py:950](../snug.py#L950), [snug.py:1188](../snug.py#L1188), [snug.py:1225](../snug.py#L1225).

### Archive members

Members are exact archive names, as with CLI `--member`. Selecting a directory member does not select its descendants. [snug.py:2310](../snug.py#L2310), [snug.py:2701](../snug.py#L2701), [snug.py:3546](../snug.py#L3546), [snug_core.py:819](../snug_core.py#L819).

| Keys | Action | Code |
|---|---|---|
| Up, Down | Move between member and action rows. | [snug.py:2384](../snug.py#L2384) |
| Space | Toggle the highlighted member; action rows are unchanged. | [snug.py:2393](../snug.py#L2393) |
| Enter, shown single-key shortcut | Activate that member or action row. | [snug.py:2391](../snug.py#L2391), [snug.py:2396](../snug.py#L2396) |
| a, n | Mark all members, or clear all marks. | [snug.py:2374](../snug.py#L2374) |
| Tab, r | Use the selection. An empty selection stays on this screen. | [snug.py:2389](../snug.py#L2389), [snug.py:2368](../snug.py#L2368) |
| Escape, b, q, Q, Ctrl+C | Discard changes and return to extraction options. | [snug.py:114](../snug.py#L114), [snug.py:2361](../snug.py#L2361), [snug.py:2387](../snug.py#L2387) |

### Extraction limits

Limits apply to the current extraction review. Returning from the limits screen with Apply keeps those edits in that review. A new extraction starts with no limits enabled. [snug.py:2663](../snug.py#L2663), [snug.py:2695](../snug.py#L2695), [snug.py:2600](../snug.py#L2600), [snug_core.py:235](../snug_core.py#L235).

| Screen | Keys | Action | Code |
|---|---|---|---|
| Limits overview | Up, Down, Enter | Move and activate a row. | [snug.py:2533](../snug.py#L2533) |
| Limits overview | e, s, f, r | Edit entry count, total bytes, bytes per member, or compression ratio. | [snug.py:2442](../snug.py#L2442), [snug.py:2544](../snug.py#L2544) |
| Limits overview | Tab, a | Apply edits and return to extraction options. | [snug.py:2541](../snug.py#L2541) |
| Limits overview | Escape, b, q, Q, Ctrl+C | Discard edits and return to extraction options. | [snug.py:114](../snug.py#L114), [snug.py:2534](../snug.py#L2534) |
| Limit field | Printable text, Backspace | Append text or erase the last character. | [snug.py:2522](../snug.py#L2522) |
| Limit field | Enter | Save a valid field value to the overview. Empty keeps the value; none clears it. Apply is still needed. | [snug.py:2505](../snug.py#L2505), [snug.py:2541](../snug.py#L2541) |
| Limit field | Escape, Ctrl+C | Discard only this field draft and return to the overview. | [snug.py:114](../snug.py#L114), [snug.py:2500](../snug.py#L2500) |

Size fields accept decimal K/KB and binary KiB units. Limit fields use a small append-and-backspace editor, rather than the line editor below. While editing a field, b, q and Q are text, not discard shortcuts. [snug_core.py:250](../snug_core.py#L250), [snug.py:2500](../snug.py#L2500), [snug.py:2529](../snug.py#L2529).

### Line prompts and history

TTY line prompts use Snug's editor. Escape and Ctrl+C cancel the current prompt. Ctrl+D on an empty line ends the session with status 0. Cancelling a setting keeps its previous value. Rejected archive paths are prefilled for editing; spaces in that path are preserved. [snug.py:1436](../snug.py#L1436), [snug.py:1681](../snug.py#L1681), [snug.py:2804](../snug.py#L2804), [snug.py:2010](../snug.py#L2010), [snug.py:1788](../snug.py#L1788), [snug.py:1818](../snug.py#L1818).

| Keys | Action | Code |
|---|---|---|
| Printable text, Space | Insert at the cursor. | [snug.py:1501](../snug.py#L1501) |
| Left, Right | Move the cursor. | [snug.py:1454](../snug.py#L1454) |
| Home, Ctrl+A; End, Ctrl+E | Move to the start or end. | [snug.py:1456](../snug.py#L1456) |
| Backspace | Delete before the cursor. | [snug.py:1460](../snug.py#L1460) |
| Delete, Ctrl+D on a nonempty line | Delete at the cursor. | [snug.py:1463](../snug.py#L1463) |
| Ctrl+K | Delete from the cursor to the end. | [snug.py:1465](../snug.py#L1465) |
| Ctrl+U | Delete before the cursor. | [snug.py:1467](../snug.py#L1467) |
| Ctrl+W | Delete the preceding whitespace and word. | [snug.py:1470](../snug.py#L1470) |
| Up, Down | Recall history for this prompt kind; Down can restore the current draft. | [snug.py:1486](../snug.py#L1486) |
| Enter | Submit the line. | [snug.py:1437](../snug.py#L1437) |
| Escape, Ctrl+C | Cancel the prompt. | [snug.py:1437](../snug.py#L1437), [snug.py:1685](../snug.py#L1685) |
| Ctrl+D on an empty line | End the session with status 0. | [snug.py:1439](../snug.py#L1439), [snug.py:1681](../snug.py#L1681), [snug.py:2804](../snug.py#L2804) |
| Tab in the archive path prompt | Browse for one file. | [snug.py:1792](../snug.py#L1792) |

Submitted nonempty lines are kept in memory by prompt kind for this process. Prompt history is not written to disk. TTY prompts do not use readline history, Ctrl+R reverse search, Ctrl+V quoted insertion, ~/.inputrc bindings, or vi mode. [snug.py:1591](../snug.py#L1591), [snug.py:1640](../snug.py#L1640), [snug.py:1631](../snug.py#L1631), [snug.py:1676](../snug.py#L1676), [snug.py:336](../snug.py#L336), [snug.py:413](../snug.py#L413).

### Results, errors and interrupts

| Keys | Action | Code |
|---|---|---|
| Up, Down | Scroll result and error details one line. | [snug.py:777](../snug.py#L777) |
| PgUp, PgDn | Scroll one page. | [snug.py:777](../snug.py#L777) |
| Home, End | Show the first or last page. | [snug.py:777](../snug.py#L777) |
| Enter, Escape, Ctrl+C | Dismiss a result or error pause and return to the main menu. | [snug.py:777](../snug.py#L777), [snug.py:114](../snug.py#L114), [snug.py:2798](../snug.py#L2798) |

Create, extract and test results show status and counts before warning details. Error causes and messages wrap; long paths shorten in the middle to keep the end of the filename visible. When details exceed the screen, a reserved row reports the hidden line count and the visible range; the key footer stays on screen at 80×24 and 40×10. [snug.py:2132](../snug.py#L2132), [snug.py:2140](../snug.py#L2140), [snug.py:2148](../snug.py#L2148), [snug.py:663](../snug.py#L663), [snug.py:690](../snug.py#L690), [snug.py:753](../snug.py#L753).

Ctrl+C acts like Escape in option menus, members, limits and line prompts. In either picker it cancels immediately, while Escape clears filtering first. At the main menu Ctrl+C quits with status 130. [snug.py:114](../snug.py#L114), [snug.py:1665](../snug.py#L1665), [snug.py:1075](../snug.py#L1075), [snug.py:1232](../snug.py#L1232), [snug.py:2824](../snug.py#L2824).

During TUI create, extract or test, Ctrl+C cancels the operation, shows a result pause and returns to the menu after dismissal. Before create publication, no archive is created and any existing archive is unchanged. If the output changed before cancellation, the pause says the current archive was kept and may already have been published; if its state cannot be verified, it asks you to check the output path. Extraction keeps files already published and removes unfinished staging where possible. Test makes no file changes. A second Ctrl+C during engine cleanup is ignored until cleanup finishes; the previous interrupt handler is then restored. CLI operation interrupts still exit 130. [snug.py:2157](../snug.py#L2157), [snug.py:2169](../snug.py#L2169), [snug.py:2180](../snug.py#L2180), [snug.py:2199](../snug.py#L2199), [snug.py:2217](../snug.py#L2217), [snug.py:4115](../snug.py#L4115).

TUI progress uses the CLI completion rules: unknown byte totals stay indeterminate until success; skipped work reaches completion without claiming skipped bytes were processed. The progress line fits the screen by terminal cell width. [snug.py:3144](../snug.py#L3144), [snug.py:3013](../snug.py#L3013), [snug.py:3086](../snug.py#L3086).

Passwords use the CLI's secure getpass prompt, not this line editor. Ctrl+C at that prompt can still end the session with status 130. Source enumeration before creation can be cancelled with Escape, q, Q or Ctrl+C. [snug.py:1866](../snug.py#L1866), [snug.py:2846](../snug.py#L2846), [snug.py:1362](../snug.py#L1362).

Extraction is transactional per regular file. Files are staged and published after decoding succeeds. A later publication failure can leave earlier files, directories or links in place. There is no whole-archive rollback. [snug_core.py:1198](../snug_core.py#L1198), [snug_core.py:2409](../snug_core.py#L2409).

### Piped and non-TTY input

With no arguments, Snug requires both standard input and standard output to be terminals. If either stream is piped, redirected or otherwise non-TTY, Snug prints command-line help and exits with status 1 without starting the menu. Use an explicit CLI command for non-interactive operation. [snug.py:4143](../snug.py#L4143), [snug.py:2864](../snug.py#L2864).

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
| `--force` | Allow replacement after complete creation. Accidental skips preserve an existing archive. Without this option, creation refuses an existing output. |
| `-q`, `--quiet` | Suppress progress and normal summaries. Accidental skip warnings and errors still appear. |

For example, to store `folder/notes.txt` as `notes.txt`:

```bash
snug create notes.zip folder/notes.txt --root folder/
```

ZIP and GZIP use levels 0–9. BZIP2 uses 1–9, with a requested 0 mapped to 1. XZ and LZMA use presets 0–9; 7z uses an LZMA2 preset when a level is supplied. Uncompressed TAR, CPIO, and AR have no compression-level effect. Higher levels generally trade time for compression; the CLI does not select arbitrary codecs.

Snug streams large regular-file payloads and supports ZIP64. In a terminal, CLI byte progress shows a percentage when the total is known. Item-only progress shows the entry counter. Unknown byte totals show processed bytes without a percentage or bar. The final line confirms completion and shows a skipped count when nonzero. Skipped work counts as processed without counting its bytes as read or written. Archive creation writes a temporary sibling file before publishing the result.

CLI progress uses stderr's terminal width. It drops speed below 80 columns, ETA or elapsed time below 60, and the bar below 40. Fields also drop when the counter needs more space. Narrow layouts shorten units and operation names to retain the counter. Member names are shortened by terminal cell width. Non-terminal stderr receives only the final progress line. `-q` suppresses progress.

If an input is omitted because of an error, Snug warns on stderr with its path and cause, even with `-q`. It prints the first 20 accidental skips and a count of any remaining skips. A published partial archive returns status 5, including when stderr is closed. The final warning gives the included and skipped input counts. [snug.py:2097](../snug.py#L2097), [snug.py:3490](../snug.py#L3490), [snug.py:3531](../snug.py#L3531).

An accidental skip prevents replacement of an existing archive, including with `--force`. Snug discards its temporary file, preserves the existing archive, and returns status 2. If no inputs could be included because of errors, Snug returns status 2 and publishes no archive. Some backends abort on input read errors instead of continuing with a partial archive. [snug_core.py:2365](../snug_core.py#L2365), [snug.py:4112](../snug.py#L4112).

Intentional omissions keep status 0. These include `--symlinks skip`, unsupported source types, and duplicate names that refer to the same real file. Different files with the same archive name cause an accidental skip. If file identity cannot be determined, Snug keeps the existing silent deduplication behavior. [snug_core.py:944](../snug_core.py#L944), [snug.py:3536](../snug.py#L3536).

## Extract archives

```bash
snug extract backup.zip -C output/
snug extract backup.tar.gz -C output/ --strip-components 1
snug extract backup.7z -C output/ --no-overwrite
snug extract archive.rar -C output/
snug extract image.iso -C image-files/ --symlinks skip
```

The default destination is the current directory. Regular files replace existing file/link targets only after their payloads succeed; a regular-file member cannot replace a conflicting directory. Prefer a new destination or use `--no-overwrite` when existing content must be retained.

TAR hardlink targets must exist among earlier archive members. A missing or later target is a corrupt archive and returns exit 2 before the destination is created. The check uses the full archive list and the extractor's name cleaning. If a selected link's target exists but is excluded by member selection or removed by `--strip-components`, Snug skips the link with a stderr warning naming both paths. The warning remains visible with `-q`, and exit stays 0. [Hardlink validation](../snug_core.py#L1083), [selection and strip handling](../snug_core.py#L1095), [warning output](../snug.py#L3558).

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

Success reports entry/file counts, decoded size, and `Archive is OK.` Failures identify the member when available and report the decoder or validation error. `--quiet` suppresses progress and the success summary. CLI progress for unknown totals stays indeterminate until decoding finishes. Unsafe member structures are rejected before payload testing.

TAR hardlinks are checked against earlier archive members before payload testing. A missing or later target fails with exit 2 and advice to obtain a complete copy. `list` and `info` can still inspect these archives. [Test preflight](../snug_core.py#L2293), [diagnostic inspection](../snug_core.py#L2258).

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

Invalid arguments and operational diagnostic errors produce plain text on stderr and leave stdout empty. They do not produce a JSON error object. An unhealthy `doctor --json` report still uses schema 1, includes `ok: false`, and returns status 2.

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
| `1` | Interactive mode lacks a terminal, a managed dependency check/repair failed, or an error could not be written to closed stderr. |
| `2` | Invalid CLI arguments, an archive/format/I/O error, or unhealthy required components reported by `doctor`. |
| `3` | Extraction or integrity testing rejected an unsafe archive. |
| `4` | An explicit extraction resource limit was exceeded. |
| `5` | An archive was created but is incomplete because inputs were skipped due to errors. [snug.py:3535](../snug.py#L3535). |
| `130` | CLI command interrupted. TUI main-menu and secure-password interruptions can exit with 130. Ctrl+C during TUI create, extract, or test shows a cancellation pause and returns to the menu. [snug.py:2232](../snug.py#L2232), [snug.py:2833](../snug.py#L2833), [snug.py:2860](../snug.py#L2860). |

Archive command errors start with `error:` on stderr. They identify the archive and the file or member when available. They state the cause and give a next step where one is clear. Interrupted CLI operations return status 130. Active progress ends before an error or interruption message.

If the stdout consumer closes its pipe early, `list`, `info`, `formats`, and `doctor` exit 0 silently. A closed consumer may receive only part of the output, including a JSON report. If stderr closes during create, extract, or test progress, Snug disables progress and continues the operation with its normal result and exit status. Progress uses no ANSI controls when stderr is not a terminal.

For error messages and fixes, see [Troubleshooting](troubleshooting.md).
