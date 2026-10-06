# Security

[← Back to README](../README.md)

Snug treats archive member paths and link targets as untrusted. Decoding backends provide entries and bytes; Snug controls extraction paths and filesystem writes. These checks reduce archive-driven path escapes, but they do not sandbox Python, py7zr, or native libarchive.

## Member paths and destination containment

Member-name validation normalizes backslashes as separators and rejects `..` traversal components, absolute paths, Windows drive paths (including drive-relative forms), and NUL bytes. This happens before `--strip-components`, so stripping does not make an unsafe name acceptable. On Windows, reserved device names, trailing dots/spaces, and alternate-data-stream components are also rejected.

Output paths are joined beneath the resolved destination. Before creating parents, Snug checks their resolved path against that root, creates them, then checks again. This prevents a pre-existing parent symlink that resolves outside the destination from redirecting a write. A supplied destination that is itself a symlink defines the resolved extraction root; links within that root are evaluated against it.

Directory entries cannot reuse an existing symlink as an output directory. File targets are removed before replacement rather than deliberately followed. The 7z writer additionally uses exclusive creation and `O_NOFOLLOW` where available, and verifies completed outputs. These checks are not a complete defense against concurrent filesystem changes; see the assumptions below.

## Symbolic links and hardlinks

Symbolic-link targets must be nonempty relative paths, without absolute/drive paths or NUL bytes. Snug resolves the target relative to the output parent and rejects links that leave the destination. Relative `..` in a link target is allowed only when the resolved result stays inside the root. Windows target components receive additional name checks.

ZIP and 7z symlink payloads are limited to 64 KiB. 7z symlinks are deferred until data writers have closed. `--symlinks skip` omits symbolic links when they are unnecessary; it does not disable all archive validation or apply to hardlinks. Junction extraction through py7zr is rejected.

Hardlink names undergo member-path and containment checks with the same stripping policy. The resolved source must be an existing regular file within the destination. Missing sources and self-links are skipped, so archive ordering or selective extraction can affect hardlink restoration. Thin AR is rejected because its members reference external files.

## Special files, overwrites, and metadata

TAR and libarchive extraction skip entries that are not supported regular files, directories, or links, including device/FIFO entries. py7zr rejects junctions and skips socket entries. Snug does not execute extracted content or package installation scripts. ZIP entries are interpreted as directories, symlinks, or regular files; ZIP type metadata is not used to create devices.

Extraction overwrites by default. A file member can replace a conflicting directory and its contents. `--no-overwrite` skips existing file/link targets and conflicting entries; existing directory metadata can still be restored. Combine it with `--no-metadata` to prevent that metadata change. Unsafe entries raise an error, while intentionally skipped entries are reported separately.

Permission restoration keeps only `0o777` bits, excluding setuid, setgid, and sticky bits. Ownership, ACLs, and extended attributes are not restored. Timestamps and permissions are best effort and can differ by platform. Extract into a new directory when isolation from existing content matters.

## Passwords and terminal output

`--password` uses `getpass` and refuses an environment where input cannot be hidden. There is no password-value argument. `--password-file` reads one UTF-8 line; only its final line ending is removed. The user owns file access controls and cleanup. Passwords remain in process memory while libraries use them; Snug does not promise secure memory erasure.

Normal output does not print passwords. Optional-backend exception text redacts occurrences of the supplied password. This does not control third-party debug logging or unrelated process inspection. Encryption support is format-specific; see [Supported formats](supported-formats.md).

Snug escapes terminal control characters in displayed filenames, paths, link targets, and errors. Backend error text also escapes nonprintable characters, preventing untrusted archive names from being emitted as terminal control sequences.

## Temporary files and backend authority

Archive creation uses a temporary `.part` file beside the destination and replaces the final path only after backend completion. Snug attempts to delete it after exceptions or interruption. A process kill or power loss can leave the temporary file behind, and atomic replacement does not guarantee durable storage after a crash. `--force` is a CLI check rather than a filesystem lock.

Extraction writes directly into the destination. A decoding or safety failure can leave partial files and already extracted entries; there is no rollback. Do not mistake a failed extraction for an untouched directory.

Third-party backends must not use unrestricted disk extraction, because that would bypass Snug's path, link, overwrite, and metadata policy. libarchive supplies entry blocks; py7zr receives Snug-controlled writers. Managed dependency downloads use separate verified staging; see [Runtime management](runtime.md#integrity-and-cleanup) for the trust boundary.

## Assumptions and known limitations

- Use a destination controlled by the current user. Another process that can change files, parent directories, links, or the archive during the operation can race path-based checks. Most native/shared writes do not use a complete descriptor-relative, race-proof filesystem API.
- There are no global quotas for decompressed size, member count, compression ratio, CPU time, or memory. Streaming regular-file data does not prevent decompression bombs or large in-memory entry tables. Use external resource limits for untrusted workloads.
- Filesystem case folding and normalization can cause collisions. The 7z path has explicit duplicate/collision checks; this is not a universal collision guarantee across every backend and filesystem.
- Native library vulnerabilities, unsupported codecs, or malformed decoder inputs remain backend risks. Download hashes verify artifacts against the lock, not the safety of their code. The repository and lock must be trusted.
- Platform privileges and available metadata determine what can be restored. Successful extraction is not a guarantee of complete backup fidelity or malware safety.

See [Troubleshooting](troubleshooting.md) for rejected paths, link privileges, and password failures.
