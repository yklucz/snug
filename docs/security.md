# Security

[← Back to README](../README.md)

Snug treats archive member paths and link targets as untrusted. Decoding backends provide entries and bytes; Snug controls extraction paths and filesystem writes. These checks reduce archive-driven path escapes, but they do not sandbox Python, py7zr, or native libarchive.

For private vulnerability reporting and supported versions, see the [Security Policy](../SECURITY.md).

## Member paths and destination containment

Member-name validation normalizes backslashes as separators and rejects `..` traversal components, absolute paths, Windows drive paths (including drive-relative forms), and NUL bytes. This happens before `--strip-components`, so stripping does not make an unsafe name acceptable. On Windows, reserved device names, trailing dots/spaces, and alternate-data-stream components are also rejected.

Extraction and integrity-test preflight validate archive names before decoding payloads. Selected outputs are checked for duplicates, normalized collisions, and file/link entries used as another member's parent. Repeated directory entries are allowed. Filesystem-specific case folding and Unicode normalization can still differ from these portable checks.

Output paths are joined beneath the resolved destination. Before creating parents, Snug checks their resolved path against that root, creates them, then checks again. This prevents a pre-existing parent symlink that resolves outside the destination from redirecting a write. A supplied destination that is itself a symlink defines the resolved extraction root; links within that root are evaluated against it.

Directory entries cannot reuse an existing symlink as an output directory. Regular files use shared, exclusively created staging files with `O_NOFOLLOW` where available. Their existing final targets are retained until atomic publication rather than deleted first. These checks are not a complete defense against concurrent filesystem changes; see the assumptions below.

## Symbolic links and hardlinks

Symbolic-link targets must be nonempty relative paths, without absolute/drive paths or NUL bytes. Snug resolves the target relative to the output parent and rejects links that leave the destination. Relative `..` in a link target is allowed only when the resolved result stays inside the root. Windows target components receive additional name checks.

ZIP and 7z symlink payloads are limited to 64 KiB. 7z symlinks are deferred until data writers have closed. `--symlinks skip` omits symbolic links when they are unnecessary; it does not disable all archive validation or apply to hardlinks. Junction extraction through py7zr is rejected.

Hardlink names undergo member-path and containment checks with the same stripping policy. Regular-file publication precedes deferred hardlink restoration, and the resolved source must be an existing regular file within the destination. Missing sources and self-links are skipped; selective extraction and hardlink-chain ordering can affect restoration. Thin AR is rejected because its members reference external files.

## Special files, overwrites, and metadata

TAR, ZIP, and libarchive extraction skip unsupported special entries, including device/FIFO entries. py7zr rejects junctions and skips socket entries. Snug does not create devices, execute extracted content, or run package installation scripts.

Extraction overwrites by default. Regular-file members cannot replace conflicting directories. `--no-overwrite` skips existing file/link targets and conflicting entries; regular-file publication uses an exclusive hard link so a target created after the early check is also retained. Filesystems that cannot perform this atomic exclusive publication fail closed. Existing directory metadata can still be restored; combine `--no-overwrite` with `--no-metadata` to prevent that change. Unsafe entries raise an error, while intentionally skipped entries are reported separately.

Regular-file permissions and timestamps are applied only after publication. Permission restoration keeps only `0o777` bits, excluding setuid, setgid, and sticky bits. Ownership, ACLs, and extended attributes are not restored. Timestamps and permissions are best effort and can differ by platform. Extract into a new directory when isolation from existing content matters.

## Passwords and terminal output

`--password` uses `getpass` and refuses an environment where input cannot be hidden. There is no password-value argument. `--password-file` reads one UTF-8 line; only its final line ending is removed. The user owns file access controls and cleanup. Passwords remain in process memory while libraries use them; Snug does not promise secure memory erasure.

Normal output does not print passwords. Optional-backend exception text redacts occurrences of the supplied password. This does not control third-party debug logging or unrelated process inspection. Encryption support is format-specific; see [Supported formats](supported-formats.md).

Snug escapes terminal control characters in displayed filenames, paths, link targets, and errors. Backend error text also escapes nonprintable characters, preventing untrusted archive names from being emitted as terminal control sequences.

## Temporary files and backend authority

Archive creation uses a temporary `.part` file beside the destination and replaces the final path only after backend completion. Snug attempts to delete it after exceptions or interruption. A process kill or power loss can leave the temporary file behind, and atomic replacement does not guarantee durable storage after a crash. `--force` is a CLI check rather than a filesystem lock.

Snug extracts regular files into temporary sibling files and commits each completed file atomically where supported. This prevents incomplete regular files from appearing at their final paths. All regular-file writers finish, flush, attempt fsync, and close before the backend returns and publication begins. Native ZIP/TAR, standalone streams, py7zr, and libarchive share this output helper. The existing final target of a regular-file member survives decoding, checksum, password, limit, write, and interruption failures before publication.

Atomicity is per regular file, not the whole extraction. A later publication failure can leave earlier committed files; directories and links can also be changed during decoding. `os.replace` publishes overwrite-enabled files, and `os.link` publishes no-overwrite files exclusively before removing their staging names. Locked files on Windows or unsupported filesystem operations can prevent publication. Best-effort file fsync does not guarantee durable directory updates after a crash.

Handled failures attempt to close and remove every staging file, preserving the original decoding/interruption error if cleanup also fails. A forced process kill, power loss, or filesystem cleanup failure can leave `.snug-part-*` files. Inspect leftover files before removing them; a failed extraction is not an untouched destination.

Third-party backends must not use unrestricted disk extraction, because that would bypass Snug's path, link, overwrite, and metadata policy. libarchive supplies entry blocks; py7zr receives Snug-controlled writers. Managed dependency downloads use separate verified staging; see [Runtime management](runtime.md#integrity-and-cleanup) for the trust boundary.

CLI application updates use the official GitHub stable-release metadata and source asset, require its GitHub SHA256 digest, restrict HTTPS download hosts, and validate bounded regular-file release contents before offline staging checks and replacement. These are integrity and installation-preservation controls, not an independent publisher signature. Only installer-owned macOS/Linux copies can update themselves; source and pip installations are refused. Windows installation remains externally managed through the PowerShell installer, while shared release checks and preferences remain available. See [Updates](updates.md) for ownership, privacy, rollback, and interruption limits.

## Resource limits and integrity checks

Extraction has no restrictive default quotas. Explicit `--max-files`, `--max-size`, `--max-file-size`, and `--max-ratio` limits check selected-entry metadata before destination creation and count actual decoded payload bytes during writes. A ratio is checked only with reliable compressed-size information; unavailable ratios are skipped while byte limits remain enforced. ZIP/7z symbolic-link payloads retain their separate 64 KiB cap. See [Usage](usage.md#extraction-limits) for units and examples.

`snug test` consumes payloads into null sinks without extracting files. It checks the archive structure and decoder/checksum failures supported by each format. Formats without payload checksums, such as plain TAR, cannot establish that all content is unchanged. Testing does not establish authenticity, malware safety, or safe behavior after extracted files are executed.

## Assumptions and known limitations

- Use a destination controlled by the current user. Another process that can change files, parent directories, links, or the archive during the operation can race path-based checks. Most native/shared writes do not use a complete descriptor-relative, race-proof filesystem API.
- Explicit extraction quotas do not bound inspection-table memory, codec memory, CPU time, or bytes decoded internally while skipping neighbors in a solid archive. Use external resource limits for untrusted workloads.
- Filesystem case folding and normalization can cause collisions beyond the shared normalized-output checks. No universal collision guarantee applies to every filesystem.
- Native library vulnerabilities, unsupported codecs, or malformed decoder inputs remain backend risks. Download hashes verify artifacts against the lock, not the safety of their code. The repository and lock must be trusted.
- Platform privileges and available metadata determine what can be restored. Successful extraction is not a guarantee of complete backup fidelity or malware safety.

See [Troubleshooting](troubleshooting.md) for rejected paths, link privileges, and password failures.
