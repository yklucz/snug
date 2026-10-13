# Pending 1.10C backlog

Audited at `fcc5aca`, with the 1.10B.8 closure fixes noted below. These are candidates and verification limits, not implementation approval. Documented choices are identified separately from defects.

## Priority follow-ups

| Item | Class | Evidence and boundary |
|---|---|---|
| Second Ctrl+C cannot escape hung TUI engine cleanup. High priority. | usability | The first SIGINT installs `SIG_IGN` until the operation scope unwinds. A second Ctrl+C is ignored during that interval. Cleanup protection is deliberate, but a hung cleanup has no in-app escape. [snug.py:2202](../snug.py#L2202), [snug.py:2210](../snug.py#L2210); `tests/test_tui_operation_cancel.py::test_second_ctrl_c_is_ignored_during_engine_cleanup` and `::test_second_real_sigint_cannot_interrupt_engine_staging_cleanup`. Preserve safe cleanup and signal restoration when reviewing alternatives. |

## Deferred progress and error work

| Item | Class | Evidence and boundary |
|---|---|---|
| Tiny-input summaries show ratios such as 6725.0%. | cosmetic | The shared create summary reports total container bytes divided by input bytes. Container overhead can exceed the input size. The TUI uses that same summary. [snug_core.py:1017](../snug_core.py#L1017), [snug.py:3408](../snug.py#L3408), [snug.py:2135](../snug.py#L2135). Review wording without changing the measured byte counts. |
| SIGINT during active CLI progress can leave an extra blank line. | cosmetic | Progress abort writes a newline and the interruption message begins with another newline. [snug.py:3137](../snug.py#L3137), [snug.py:45](../snug.py#L45), [snug.py:4111](../snug.py#L4111); `tests/test_cli_progress_cleanup.py::test_cli_failure_terminates_only_an_active_tty_progress_line`. Keep error separation and quiet-mode behavior. |
| CLI and TUI have separate Unicode cell-width helpers. | cosmetic | [snug_core.py:107](../snug_core.py#L107), [snug_core.py:113](../snug_core.py#L113), [snug.py:424](../snug.py#L424), [snug.py:432](../snug.py#L432). Consider sharing the width model without changing rendering contracts. This does not establish native emoji widths. |
| Optional 7z creation has long gaps without activity updates. | usability | py7zr exposes no input-byte callbacks here. Snug updates an item counter before each blocking write, with no heartbeat during the write. [snug_ext.py:762](../snug_ext.py#L762), [snug_ext.py:769](../snug_ext.py#L769). The counter is now honest; do not fabricate bytes or ETA. |
| Update download activity is not displayed. | usability | The download loop has no progress callback or renderer. [snug_update.py:447](../snug_update.py#L447). Live network update, download, and cancellation remain unverified. Keep installation and verification policy separate from presentation. |
| Generic TUI errors still use backend wording. | usability | Result pauses now wrap and scroll, but ordinary errors still print `_safe(exc)` rather than the CLI contextual formatter. [snug.py:2811](../snug.py#L2811), [snug.py:2816](../snug.py#L2816), [snug.py:3388](../snug.py#L3388). Archive context, backend internals, and recovery advice need their own review. |
| Quoted paths containing the opposite quote can wrap rather than shorten as a whole path. | cosmetic | The result-path recognizer excludes both quote characters inside either quoted form. A long apostrophe-containing filename displayed inside double quotes can miss that recognizer. Wrapped text remains reachable by scrolling. [snug.py:707](../snug.py#L707), [snug.py:727](../snug.py#L727), [snug.py:738](../snug.py#L738). Keep this separate from literal input parsing and preserve complete causes. |

## Still-open 1.10A.11 items

| Item | Class | Current evidence |
|---|---|---|
| Password controls for TUI list and info. | usability | Both engine calls omit the password argument. [snug.py:2747](../snug.py#L2747), [snug.py:2764](../snug.py#L2764). Create, extract, and test already have password flows. |
| Destination browsing. | usability | Destination selection uses a text prompt without an archive-picker browse action. [snug.py:2621](../snug.py#L2621). |
| Inspection viewer. | usability | The menu offers create, extract, list, info, and test, without a separate inspection viewer. [snug.py:2779](../snug.py#L2779). |
| Explicit closed-symlink-directory label in the source picker. | cosmetic | The picker refuses entry and uses a generic `@` marker. The single-archive picker has a separate explanatory notice. [snug.py:829](../snug.py#L829), [snug.py:855](../snug.py#L855), [snug.py:1222](../snug.py#L1222). |
| Enter on a file accepts the marked set. Documented choice to review. | usability | Enter confirms and marked paths take priority. [snug.py:1089](../snug.py#L1089), [snug.py:1203](../snug.py#L1203). |
| Internal non-terminal limits helper applies edits on `b`; the TTY screen discards them. | scripted-use | [snug.py:2563](../snug.py#L2563), [snug.py:2533](../snug.py#L2533). Public no-argument startup rejects non-terminal stdin or stdout, [snug.py:2865](../snug.py#L2865), so this helper discrepancy is not an exposed piped menu. |
| Empty Ctrl+D and EOF quit the session. Documented choice to review. | usability | [snug.py:1439](../snug.py#L1439), [snug.py:1681](../snug.py#L1681). Preserve the distinction between EOF and operation cancellation. |
| Whole emoji grapheme width and editing. | usability | Widths and editor boundaries do not group complete ZWJ, flag, or modifier sequences. [snug.py:424](../snug.py#L424), [snug.py:1411](../snug.py#L1411). Tests preserve valid codepoints, not whole grapheme clusters or physical glyph width. |
| Path completion. | usability | The editor handles printable characters; archive Tab is a browser action. [snug.py:1501](../snug.py#L1501), [snug.py:1790](../snug.py#L1790). |
| TUI quote and unescape handling. | usability | Manual archive input passes literal text to `Path`. [snug.py:1817](../snug.py#L1817). Any convenience parsing must preserve literal quote characters in filenames. |
| Bracketed and multiline paste. | usability | Terminal modes do not enable bracketed paste, and newline submits input. [snug.py:75](../snug.py#L75), [snug.py:1437](../snug.py#L1437). |
| Output and destination prompts strip edge spaces. | usability | The default prompt strips input; these callers use that default. [snug.py:1665](../snug.py#L1665), [snug.py:1693](../snug.py#L1693), [snug.py:2013](../snug.py#L2013), [snug.py:2621](../snug.py#L2621). Archive input already preserves spaces. |
| Picker-filter Backspace removes one combining codepoint. | usability | [snug.py:1112](../snug.py#L1112). The text editor has different base-plus-mark boundaries, [snug.py:1411](../snug.py#L1411). |
| Secure-password Ctrl+C ends the session. Documented choice to review. | usability | Secure input delegates to getpass and retains session interruption rather than local setting cancellation. [snug.py:1875](../snug.py#L1875), [snug.py:3214](../snug.py#L3214), [snug.py:2859](../snug.py#L2859). Preserve hidden input and secret redaction. |

The early create staging residue, older-Python long-path input errors, and stale exit-status docs are resolved in the 1.10B.8 commit `4cbff3e`. See [create staging](../snug_core.py#L2140), [input probes](../snug.py#L815), [exit status](usage.md#exit-status), `tests/test_create_staging.py`, and `tests/test_path_probes.py`.

The 1.10A.11 scrolling and narrow-list-name items are resolved by `fcc5aca`, [snug.py:690](../snug.py#L690), [snug.py:756](../snug.py#L756), and `tests/test_tui_results.py::test_overflow_position_and_every_scroll_key_keep_all_rows_reachable`. Both Pyright findings were resolved by `e3f9a72`, [snug.py:2504](../snug.py#L2504) and [snug.py:2641](../snug.py#L2641).

## Resolved in 1.10C.2

These changes remain uncommitted.

| Item | Result and evidence |
|---|---|
| TAR hardlink validation and selection warnings. | Missing or later targets fail `test` and `extract` with exit 2 before destination creation. Selection uses the full archive list. A selected link with an excluded or stripped target is skipped with a warning and exit 0. `list` and `info` remain available. [Earlier-member check](../snug_core.py#L1083), [omitted-source warnings](../snug_core.py#L1095); `tests/test_tar_hardlinks.py`. |
| Error advice and unsafe hardlink context. | All-skipped create, preserved partial replacement, and incomplete publication give corrective advice. Their exits remain 2, 2, and 5. Directory conflicts suggest an empty destination or renaming or removing the existing directory. Unsafe hardlinks name both paths and retain exit 3. [Advice](../snug.py#L3361), [incomplete warning](../snug.py#L3531), [hardlink context](../snug_core.py#L1148); `tests/test_c2_error_advice.py`. |
| Unreadable ZIP without libarchive. | Native probing propagates permission errors. Python 3.13.5 now reports permission advice without an optional backend. Its previously failing unreadable-archive test passes unchanged. [ZIP probe](../snug_core.py#L1523); `tests/test_zip_permissions.py`. |

## Verification work

| Item | Class | Limit |
|---|---|---|
| Current Linux and other supported Python versions. | usability | macOS Python 3.14.0 passes 2,529 tests without skips after 1.10C.2. Local Python 3.13.5 passes 2,348 tests with 181 skips and no failures. All 49 new checks pass on both runtimes. Python 3.10 to 3.12 and current Linux execution remain unverified. |
| Linux arm64 CI. | usability | Linux arm64 CI is unverified until it runs on GitHub; arm64 hosted runners may be unavailable for private repositories. [New workflow job](../.github/workflows/tests.yml#L22). |
| Native Terminal.app and Ghostty glyph, cursor, wrapping, and flicker checks. | cosmetic | Automated width and PTY byte checks do not establish physical font rendering or complex emoji width. [snug.py:424](../snug.py#L424); `tests/test_tui_progress.py::test_unicode_width_resize_and_throttle`. |
| Physical disk-full and durability behavior. | safety | No disk-filling experiment was run. fsync remains best effort and suppresses OSError. [snug_core.py:1216](../snug_core.py#L1216); `tests/test_transactional.py::test_best_effort_fsync_failure_does_not_discard_valid_payload`. This is a verification limit, not a demonstrated data-loss defect. |

## Tiny-file performance proposals

No optimization was implemented. The [full profile report](profile-1.10c2-tiny-files.md) records frozen commit `4f77a2b`, the top ten cumulative costs, exclusive cost buckets, and safety boundaries.

On macOS arm64 with Python 3.14.0, 100,000 one-byte files took 8.120 seconds to create as a native TAR and 32.461 seconds to extract with Snug. Plain Python writing took 5.609 seconds. System tar extracted the same archive in 6.689 seconds. Every output name and byte was verified outside timing. These were single runs with existing caches. The comparators do not establish an equivalent safety contract.

cProfile increased creation to 12.608 seconds and extraction to 52.798 seconds. The ceilings below are overlapping instrumented component costs. They are not achieved gains or predictions of unprofiled savings. Each proposal needs its own benchmark and safety checks before approval.

| Proposal | Estimated ceiling | Safety boundary |
|---|---|---|
| Reuse immutable cleaned names and lexical paths across TAR passes. | 4.92 seconds, 9.3% of profiled extraction. | Keep fresh realpath checks. Preserve per-file transactions, fsync, staging, publication order, and containment. |
| Remove duplicate regular-file target preparation before SafeOutputFile repeats it. | 5.28 seconds, 10.0% of profiled extraction. | Retain constructor preparation and publication checks. This removes an early repeated check. Verify path mutation cases under the existing parent-concurrency limitation. |
| Use a no-op progress sink for quiet operations. | 0.278 seconds extraction; 0.214 seconds creation. | Preserve all safety checks and normal progress. No-op calls still cost time. |
| Avoid abort or unlink work for already committed outputs. | 0.341 seconds, 0.65% of profiled extraction. | Keep cleanup mandatory for every unpublished staging file. Preserve transactions, fsync, staging, publication order, and containment. |
| Replace repeated source type and size probes with one fresh lstat. | 0.694 seconds, 5.5% of profiled creation. | Keep backend source checks. Preserve archive staging, publication order, and containment. At least one probe remains. |

Removing staging, fsync, or atomic publication would change safety guarantees. Reusing realpath results across filesystem mutation phases would weaken fresh containment checks. Those changes are not proposed.
