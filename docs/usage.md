# Usage

[← Back to README](../README.md)

## Interactive mode and command reference

```powershell
snug
snug --help
snug --version
snug create --help
snug extract --help
snug list --help
snug info --help
snug update --help
```

With no arguments, Snug opens a terminal menu for creation, extraction, listing, and information. Use arrow keys and Enter to choose an action; `q` exits. The creation picker supports directory navigation, Space to mark sources, and `/` to filter. It offers formats writable by installed backends. Password options are available through subcommands rather than interactive prompts.

Interactive mode requires terminal stdin and stdout. When invoked without arguments in a pipe or redirected session, Snug prints help and exits with status 1. For automation, use the subcommands with their required arguments:

```text
snug create ARCHIVE SOURCE [SOURCE ...] [options]
snug extract ARCHIVE [options]
snug list ARCHIVE [options]
snug info ARCHIVE [options]
snug update [--check | --enable-checks | --disable-checks]
```

Use the full command name. `snug folder/`, `snug archive.zip`, `snug file.txt`, and single-letter aliases are invalid; Snug does not infer an operation from a path. `extract` accepts one archive per command. `create` already accepts multiple files and directories.

## Paths inserted by terminals

Dragging a file into a terminal may insert its quoted or escaped path. Keep the explicit command and pass that path as one argument:

```powershell
snug create backup.zip ".\My Folder"
snug extract ".\My Archive.7z" -C "restored files/"
snug create notes.zip "Tài liệu 📦/" "author's notes.txt" "notes (draft) [final].txt"
```

Quoting is handled by your shell; Snug receives argument values directly and does not evaluate path strings as shell commands. Spaces, Unicode, Vietnamese text, emoji, apostrophes, dots, parentheses, and brackets are preserved subject to Windows filesystem limits. Double quotes delimit shell arguments; Windows forbids double quotes within physical filenames.

For Unicode filenames in redirected Python output, select UTF-8 with `$env:PYTHONIOENCODING = 'utf-8'` and have the receiving program decode UTF-8. This matters for older Windows Python versions whose pipe encoding otherwise follows the system locale.

For positional filenames beginning with `-`, use the normal `--` separator. Put options before the separator:

```powershell
snug create archive.zip -- -important-file.txt
snug create -q -- -backup.zip -important-file.txt
snug list -- -backup.zip
snug info -- -backup.zip
snug extract -C output/ -- -backup.zip
```

For a leading-hyphen option value, use `=` or an explicit relative path: `snug extract --directory=-output -- -backup.zip` or `snug extract -C ./-output -- -backup.zip`. Quoting a leading hyphen alone does not stop argparse from treating it as an option. All paths after `--` are positional arguments.

## Create archives

```powershell
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

```powershell
snug create notes.zip folder/notes.txt --root folder/
```

ZIP and GZIP use levels 0–9. BZIP2 uses 1–9, with a requested 0 mapped to 1. XZ and LZMA use presets 0–9; 7z uses an LZMA2 preset when a level is supplied. Uncompressed TAR, CPIO, and AR have no compression-level effect. Higher levels generally trade time for compression; the CLI does not select arbitrary codecs.

Snug streams large regular-file payloads and supports ZIP64. Native and libarchive operations report byte progress, speed, and ETA; 7z creation reports item progress because py7zr does not provide input-byte callbacks. Archive creation writes a temporary sibling file and replaces the destination after completion.

## Extract archives

```powershell
snug extract backup.zip -C output/
snug extract backup.tar.gz -C output/ --strip-components 1
snug extract backup.7z -C output/ --no-overwrite
snug extract archive.rar -C output/
snug extract image.iso -C image-files/ --symlinks skip
```

The default destination is the current directory. Existing entries can be replaced by default, including a directory that conflicts with a file member. Prefer a new destination or use `--no-overwrite` when existing content must be retained.

| Option | Behavior |
|---|---|
| `-C`, `--directory DIR` | Select the destination directory, creating it if necessary. |
| `-m`, `--member NAME` | Extract an exact member name; repeat to select more. Names match archive paths before stripping. This is not a glob or recursive directory selector. |
| `--strip-components N` | Drop N leading path components; members with no remaining components are omitted. N must be nonnegative. |
| `--no-overwrite` | Skip existing file/link targets and conflicting entries. Existing directories may still receive restored metadata unless `--no-metadata` is also set. |
| `--no-metadata` | Do not restore archived permissions or timestamps. |
| `--symlinks store\|skip` | Create validated links by default, or skip symbolic links. There is no extraction `follow` mode. |
| `-q`, `--quiet` | Suppress progress and summary output. |

List first to obtain exact member names:

```powershell
snug list backup.zip
snug extract backup.zip -C selected/ --member folder/notes.txt --member folder/photo.jpg
```

Extraction detects content signatures before falling back to the suffix. A ZIP renamed to `.bin` can still be read:

```powershell
snug create backup.bin folder/ --format zip
snug info backup.bin
snug extract backup.bin -C output/
```

There is no extraction `--format` override. A corrupt header or a recognized suffix does not guarantee that a reader can decode the archive.

## Standalone compression

Each standalone stream contains exactly one regular-file payload in Snug. Use one source file per command:

```powershell
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

```powershell
snug list backup.7z
snug list backup.zip --verbose
snug info backup.7z
```

`list` prints member names. `--verbose` adds entry type, permissions, size, modification time, and link target when available. `info` reports the detected format, selected backend, archive size, entry counts, uncompressed size, and current create/extract capabilities. Backends add encryption information only when their APIs establish it. These commands do not guarantee that every data member can be extracted successfully.

## Passwords

All four subcommands accept either `--password` or `--password-file FILE`; the options are mutually exclusive. `--password` takes no value and prompts securely:

```powershell
snug create protected.7z folder/ --password
snug extract protected.7z -C output/ --password
snug list protected.7z --password
snug info protected.7z --password
```

Use an existing UTF-8 file containing one password line for noninteractive commands:

```powershell
snug extract protected.7z -C output/ --password-file password.txt
```

The final LF or CRLF is removed; spaces are preserved, and embedded line breaks are rejected. Snug refuses a prompt that cannot hide input. It does not print passwords as part of normal output, and optional-backend error messages redact the supplied password. Protect password files yourself; see [Security](security.md#passwords-and-terminal-output).

Only 7z creation supports password protection. Traditional encrypted ZIP can be extracted by the native reader. TAR and standalone streams have no password support, and encrypted RAR extraction is unsupported. See [Supported formats](supported-formats.md) for backend-specific limits.

## Updates

```powershell
snug update --check
snug update
snug update --disable-checks
snug update --enable-checks
```

Checks report the latest stable release. On Windows, `snug update` reports that installation is managed externally and leaves the installation unchanged. Update a managed copy by rerunning the Windows PowerShell installer; use the original installation method for source/pip copies. Automatic checks never install a release. See [Updates](updates.md) for network behavior, check interval, state, and failed-update recovery.

## Exit status

| Status | Meaning |
|---|---|
| `0` | Command completed; check summaries for skipped entries. |
| `1` | Interactive mode lacks a terminal, or a managed dependency check/repair failed. |
| `2` | Invalid CLI arguments or an archive/format/I/O error. |
| `3` | Extraction rejected an unsafe archive. |
| `130` | Interrupted with Ctrl+C. |

For error messages and fixes, see [Troubleshooting](troubleshooting.md).
