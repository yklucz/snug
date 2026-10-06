# Archive fixtures

[Development guide](../../docs/development.md) · [Supported formats](../../docs/supported-formats.md)

These small regression archives are vendored so tests run without network
access. [manifest.json](manifest.json) records their byte sizes, SHA-256 hashes, and exact
upstream URLs. They contain test data rather than software to execute.

Archive extensions are marked as binary in [.gitattributes](../../.gitattributes)
to preserve their verified bytes across platforms. Add a matching rule when
introducing a fixture with a new extension.

The `test_read_format_*` files come from the official
[libarchive v3.8.1 test suite](https://github.com/libarchive/libarchive/tree/v3.8.1/libarchive/test).
They were downloaded as `.uu` files and decoded using Python's
`binascii.a2b_uu`. The ISO fixture was additionally decompressed from Unix
`.Z` using `uncompress`; the resulting image is stored directly to avoid
requiring that decompressor in the tests.

The upstream distribution license is retained verbatim in
[LIBARCHIVE-COPYING](LIBARCHIVE-COPYING); copyright and license notices from the associated
upstream test sources are retained in [LIBARCHIVE-NOTICES](LIBARCHIVE-NOTICES).

| Fixture | Purpose |
|---|---|
| `test_read_format_7zip_lzma1_2.7z` | Independent 7z extraction via either backend |
| `test_read_format_rar.rar` | RAR files, directories, and a safe symlink |
| `test_read_format_rar5_compressed.rar` | RAR5 compressed binary payload |
| `test_read_format_cab_1.cab` | CAB file extraction |
| `test_read_format_iso_2.iso` | ISO directories and two text files |
| `test_read_format_zip_ppmd8.zipx` | ZIPX PPMd, unavailable in Python's ZIP reader |
| `test_read_format_lha_header0.lzh` | LHA/LZH files and safe symlinks |
| `test_read_format_warc.warc` | WARC resources, including block padding |
| `test_read_format_cpio_svr4_gzip_rpm.rpm` | RPM wrapper with compressed CPIO payload |
| `sample.xar` | XAR with `nested/hello.txt` containing `XAR round trip\n` |
| `sample-traditional.zip` | Traditional encrypted ZIP extraction and password failures |

`sample.xar` was generated locally with libarchive-c 5.3 and native
libarchive 3.7.4, independently of Snug's archive engine:

```python
with libarchive.file_writer("sample.xar", "xar") as archive:
    archive.add_file_from_memory("nested/hello.txt", 15, b"XAR round trip\n")
```

The locally generated XAR and its trivial test content are provided under
the repository's MIT license. The creation test suite also independently
generates adversarial ZIP/TAR/CPIO/7z archives and an AR/DEB container at
runtime; no malicious fixture is extracted with unrestricted backend APIs.

`sample-traditional.zip` is a locally generated traditional encrypted ZIP,
provided under the repository's MIT license. Its test-only password is
`fixture-password`, and `hello.txt` contains `ZIP password fixture\n`.
It was generated independently with libarchive-c 5.3 and native libarchive
3.8.9 using `file_writer(..., 'zip', options='zip:encryption=traditional',
passphrase='fixture-password')`. Native ZIP tests read it with the standard
library backend, including missing/incorrect-password failures.
