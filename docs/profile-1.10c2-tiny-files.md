# Tiny-file profile for 1.10C.2

The benchmark used Python 3.14.0 on macOS 27.0.1 arm64 and the native TAR backend. It used 100,000 flat regular files named f000000 through f099999. Every file contained one byte, x. Source timestamps were fixed after the plain-write timing. Snug used ProgressDisplay(quiet=True), overwrite=True, and preserve_metadata=True. This profiles ArchiveEngine operations, including collection and inspection. CLI parsing and terminal rendering are outside the measurement.

The benchmark froze snug.py, snug_core.py, snug_ext.py, snug_runtime.py, and snug_update.py from commit 4f77a2b. Concurrent implementation edits therefore could not change the measured code. No full suite ran during the benchmarks. Small focused checks and editing could still cause occasional contention. The system had 219 GiB free on the APFS data volume. Caches were not flushed. These are single-run measurements, not statistical performance claims.

| Operation | Unprofiled wall time | cProfile wall time |
|---|---:|---:|
| Plain Python writes | 5.609 s | Not profiled |
| Snug TAR creation, including collection | 8.120 s | 12.608 s |
| Snug TAR extraction, including inspection | 32.461 s | 52.798 s |
| /usr/bin/tar extraction of the same archive | 6.689 s | Not profiled |

The Snug report's narrower timed phases were 5.872 s for create and 30.458 s for extract. cProfile increased the whole-operation wall times by 1.553x for creation and 1.627x for extraction. Instrumented cumulative times are not unprofiled wall times. The archive was 204,810,240 bytes. Both Snug creation runs produced byte-identical archives with SHA256 004221f8ab53c6d8aa3f064429d9d484928a806bee73a60e9bff159db5486aca. Native extraction, plain writing, and bsdtar extraction all produced 100,000 regular files and 100,000 payload bytes. Each filename and file content was verified after timing. The shared name/content SHA256 was 205fd9b9c819c40604b76911cb0b939a027770d202976472db7c8b0e74308ce8. Verification and cleanup times were excluded.

The plain writer only makes the destination and opens, writes, and closes each final filename. It has no per-file fsync, staging, deferred atomic publication, containment validation, or metadata restoration. System tar is bsdtar 3.5.3 with libarchive 3.7.4. It read exactly the same Snug-produced TAR. Neither comparator proves an equivalent safety contract.

Top ten cumulative costs follow. Benchmark wrapper functions were omitted. Rows overlap and must not be added. Source lines refer to frozen commit 4f77a2b.

| Creation function | File:line | Cumulative |
|---|---|---:|
| ArchiveEngine.create | snug_core.py:2272 | 12.557 s |
| NativeBackend.create | snug_core.py:1562 | 9.394 s |
| NativeBackend._write_tar | snug_core.py:1582 | 9.394 s |
| NativeBackend._add_to_tar | snug_core.py:1598 | 9.178 s |
| TarFile.addfile | Python 3.14 tarfile.py:2342 | 6.664 s |
| TarInfo.tobuf | Python 3.14 tarfile.py:1034 | 3.483 s |
| TarInfo.create_pax_header | Python 3.14 tarfile.py:1078 | 3.379 s |
| ArchiveEngine._collect_items | snug_core.py:2263 | 3.163 s |
| TarInfo._create_header | Python 3.14 tarfile.py:1158 | 2.653 s |
| _iter_items | snug_core.py:944 | 2.425 s |

| Extraction function | File:line | Cumulative |
|---|---|---:|
| ArchiveEngine.extract | snug_core.py:2356 | 52.798 s |
| NativeBackend.extract | snug_core.py:1571 | 34.899 s |
| NativeBackend._extract_tar | snug_core.py:1794 | 34.890 s |
| NativeBackend._extract_tar_member | snug_core.py:1809 | 28.296 s |
| NativeBackend._extract_tar_file | snug_core.py:1848 | 25.410 s |
| _extract_entry | snug_core.py:1291 | 25.080 s |
| _extract_regular_file | snug_core.py:1359 | 17.244 s |
| SafeOutputFile.commit | snug_core.py:1225 | 12.171 s |
| SafeOutputFile.__init__ | snug_core.py:1163 | 12.037 s |
| _check_inside | snug_core.py:758 | 11.215 s |

Grouped exclusive times follow. These account for function self time only. The syscall categories include all callers and cannot attribute every stat or lstat to a directory check. Separate cumulative landmarks below show the inclusive cost of semantic operations.

| Bucket | Creation self time | Extraction self time | Extraction share |
|---|---:|---:|---:|
| Stage os.open, fdopen, random-name generation | 0.000111 s | 4.934 s | 9.35% |
| io.open, mainly source opens for create and fd wrapping for extract | 1.564 s | 0.175 s | 0.33% |
| fsync | 0 | 1.839 s | 3.48% |
| Buffered flush | 0 | 0.726 s | 1.38% |
| Atomic replace or link | 0.000127 s | 5.086 s | 9.64% |
| Snug lexical validation and containment wrappers | 0 | 1.373 s | 2.60% |
| realpath own Python work | 0.299 s | 1.603 s | 3.04% |
| All stat, lstat, and mkdir syscalls | 2.027 s | 5.418 s | 10.26% |
| pathlib status and mkdir wrappers | 0.088 s | 0.190 s | 0.36% |
| Quiet progress display methods | 0.120 s | 0.150 s | 0.28% |
| ProgressReader wrapper | 0.081 s | 0 | 0% |
| SafeOutputFile methods, excluding their callees | 0 | 0.580 s | 1.10% |
| Metadata chmod and utime | 0 | 3.670 s | 6.95% |
| Cleanup unlink | 0 | 0.283 s | 0.54% |

Extraction accounted for 52.783 s in cProfile. Creation accounted for 12.606 s. Staged-descriptor io.open includes 14 additional archive/probe opens. They are negligible in its 100,014 calls. Extraction called fsync and replace 100,000 times each. realpath ran 500,001 times. mkdir ran 200,001 times. lstat ran 2,500,005 times; stat ran 400,002 times. Creation used one archive mkstemp and one replace, no SafeOutputFile, and no fsync call.

Inclusive landmarks overlap the buckets above. _prepare_target ran 200,000 times and cost 10.563 s. _check_inside ran 500,000 times and cost 11.215 s. realpath cost 6.297 s including its callees. _resolve_member ran 300,000 times and cost 7.372 s. SafeOutputFile construction cost 12.037 s. seal ran 200,000 times but issued only 100,000 fsync calls; its total was 2.956 s. commit cost 12.171 s. abort cost 0.341 s. mkdir syscalls alone cost 0.267 s. Most directory preparation time is fresh containment checking, rather than mkdir itself. Progress item and chunk callbacks, including their children, cost 0.278 s for extract and 0.214 s for create.

Proposals are report only. Gain estimates are component upper bounds measured under cProfile. They are not achieved improvements or predictions of unprofiled savings. They overlap and must not be added. Prototype benchmarks and adversarial safety tests are required before adopting them.

| Proposed optimization | Estimated gain ceiling | Per-file transactions | fsync | Staging | Publication order | Path containment |
|---|---|---|---|---|---|---|
| Reuse immutable cleaned names and resolved lexical output paths across the three TAR passes. Retain the same cleaning rules and every fresh realpath check. See snug_core.py:704, 734, 1794, 1809, 1291. | At most two thirds of _resolve_member's cost, 4.92 s or 9.3% of profiled extraction. Residual reuse costs reduce this. | Unchanged | Unchanged | Unchanged | Unchanged | Unchanged if fresh checks remain at every current filesystem phase. |
| Remove the duplicate regular-file _prepare_target call immediately before SafeOutputFile does the same preparation. Keep constructor preparation and commit containment checks. See snug_core.py:1291, 1163, 1225. | At most half of _prepare_target's cost, 5.28 s or 10.0% of profiled extraction. | Unchanged | Unchanged | Unchanged | Unchanged | Fewer redundant early checks. No intended guarantee change under the existing rule that parents must not be concurrently moved. Must verify against path mutation tests. |
| Use a no-op progress sink for quiet operations. Keep normal progress unchanged. See snug_core.py:557, 594, 600. | At most 0.278 s or 0.53% of profiled extraction; 0.214 s or 1.7% of profiled creation. No-op calls still have a cost. | Unchanged | Unchanged | Unchanged | Unchanged | Unchanged |
| Avoid abort/unlink on already committed outputs. Keep cleanup mandatory for every unpublished output. See snug_core.py:1254, 1271. | At most 0.341 s or 0.65% of profiled extraction. | Unchanged | Unchanged | Unpublished cleanup retained | Unchanged | Unchanged |
| Collapse _file_size's repeated type and size probes into one fresh lstat for stored regular files. Keep the backend's fresh source checks. See snug_core.py:934, 2263, 1598. | At most the 0.694 s _file_size cost, 5.5% of profiled creation. One probe remains, so actual gain is smaller. | Unchanged | Unchanged | Archive staging retained | Unchanged | Unchanged |

Do not remove staging, per-file fsync, or atomic publication for speed. The combined exclusive staging and fd-wrap cost is about 5.11 s; fsync is 1.84 s; replace is 5.09 s under cProfile. Eliminating those costs would change the named safety guarantees. Caching realpath results across filesystem mutation phases would weaken fresh path containment checks. None of these changes was implemented.

The disposable harness performed these operations:

1. Write the 100,000 source files with plain Python and measure wall time.
2. Create a native TAR with `ArchiveEngine.create` and measure wall time.
3. Extract it with `ArchiveEngine.extract` and measure wall time.
4. Extract the same TAR with `/usr/bin/tar -xf` and measure wall time.
5. Repeat Snug create and extract with `cProfile.Profile.runcall`.
6. Verify file names, counts, payload bytes, and hashes outside timing.
7. Analyze both profiles with `pstats.Stats` by cumulative and self time.

The raw profiles, benchmark harness, frozen modules, source files, archives, and output trees were disposable. They were removed after these findings were recorded. No optimization was implemented.

Current source references locate the corresponding operations. The measurements above refer to frozen commit `4f77a2b`.

- [Path cleaning](../snug_core.py#L704), [member resolution](../snug_core.py#L734), and [target preparation](../snug_core.py#L748).
- [SafeOutputFile construction](../snug_core.py#L1207), [sealing](../snug_core.py#L1255), [publication](../snug_core.py#L1269), and [cleanup](../snug_core.py#L1298).
- [Regular-file extraction](../snug_core.py#L1408), [TAR writing](../snug_core.py#L1633), and [TAR extraction](../snug_core.py#L1845).
- [Source collection](../snug_core.py#L2317), [archive creation](../snug_core.py#L2325), and [archive extraction](../snug_core.py#L2409).
