# Remaining backlog after 1.10.0

These are future work candidates and verification limits. They do not authorize implementation. Documented choices are identified separately from defects. Resolved 1.10 work is recorded in the [release notes](release-notes-1.10.0.md).

## Usability

| Item | Evidence and boundary |
|---|---|
| Second Ctrl+C cannot escape hung TUI engine cleanup. High priority. | The first interrupt installs `SIG_IGN` until the operation scope unwinds. Preserve cleanup and signal restoration when reviewing alternatives. [Signal scope](../snug.py#L2199); `tests/test_tui_operation_cancel.py::test_second_ctrl_c_is_ignored_during_engine_cleanup` and `::test_second_real_sigint_cannot_interrupt_engine_staging_cleanup`. |
| Optional 7z creation can have long gaps without activity updates. | Snug advances an item counter before each blocking py7zr write. No input-byte callback or heartbeat is provided here. Do not fabricate bytes or ETA. [Write loop](../snug_ext.py#L762). |
| Update download activity is not displayed. | The download loop has no progress callback or renderer. Live download and cancellation remain unverified. Keep update policy separate from presentation. [Download loop](../snug_update.py#L447). |
| Generic TUI errors still use backend wording. | Result pauses wrap and scroll, but ordinary errors print `_safe(exc)` rather than the CLI contextual formatter. Archive context and advice need a separate review. [TUI errors](../snug.py#L2809), [CLI formatter](../snug.py#L3403). |
| Password controls for TUI list and info. | Both engine calls omit a password argument. Create, extract and test already have password flows. [List](../snug.py#L2748), [info](../snug.py#L2765). |
| Destination browsing. | Destination selection uses a text prompt. [Destination prompt](../snug.py#L2622). |
| Inspection viewer. | The menu offers create, extract, list, info and test without a separate inspection viewer. [Menu actions](../snug.py#L2780). |
| Enter on a file accepts the marked set. Documented choice to review. | Enter confirms and marked paths take priority. `tests/test_tui_picker.py::test_confirm_accepts_exact_sorted_marks_including_hidden_and_other_directories`; [picker handling](../snug.py#L1089). |
| Empty Ctrl+D and EOF quit the session. Documented choice to review. | Preserve the distinction between EOF and operation cancellation. [EOF handling](../snug.py#L1681); `tests/test_tui_interrupt.py::test_secure_getpass_and_tty_line_prompt_keep_separate_interrupt_contracts`. |
| Whole emoji grapheme width and editing. | Width and editor boundaries do not group complete ZWJ, flag or modifier sequences. Tests preserve codepoints rather than physical glyph width. [Cell width](../snug.py#L432), [editor boundaries](../snug.py#L1411). |
| Path completion. | The editor handles printable characters. Archive Tab opens a browser. [Archive prompt](../snug.py#L1788). |
| TUI quote and unescape handling. | Manual archive input passes literal text to `Path`. Convenience parsing must preserve literal quote characters in filenames. [Path conversion](../snug.py#L1817). |
| Bracketed and multiline paste. | Terminal setup does not enable bracketed paste. Newline submits input. [Terminal setup](../snug.py#L202), [editor handling](../snug.py#L1437). |
| Output and destination prompts strip edge spaces. | These callers use the prompt's default stripping. Archive input already preserves spaces. [Prompt return](../snug.py#L1693), [output prompt](../snug.py#L2013), [destination prompt](../snug.py#L2622). |
| Picker-filter Backspace removes one combining codepoint. | The line editor uses different base-plus-mark boundaries. [Filter editing](../snug.py#L1112), [line editor](../snug.py#L1411). |
| Secure-password Ctrl+C ends the session. Documented choice to review. | Secure input delegates to getpass and retains session interruption. Preserve hidden input and secret redaction. [Password prompt](../snug.py#L1866); `tests/test_tui_interrupt.py::test_secure_getpass_and_tty_line_prompt_keep_separate_interrupt_contracts`. |

## Cosmetic

| Item | Evidence and boundary |
|---|---|
| Tiny-input summaries can show ratios such as 6725.0%. | The ratio is container bytes divided by input bytes. Overhead can exceed input size. Review wording without changing measured byte counts. [Ratio](../snug_core.py#L1046), [shared summary](../snug.py#L3422). |
| SIGINT during active CLI progress can leave an extra blank line. | Progress abort writes a newline and the interruption message begins with another. Preserve error separation and quiet behavior. [Interruption text](../snug.py#L45), [CLI interruption](../snug.py#L4134); `tests/test_cli_progress_cleanup.py::test_cli_failure_terminates_only_an_active_tty_progress_line`. |
| CLI and TUI have separate Unicode cell-width helpers. | Consider sharing their width model without changing rendering contracts. This does not establish native emoji width. [CLI width](../snug_core.py#L107), [TUI width](../snug.py#L432). |
| Quoted paths containing the opposite quote can wrap instead of shortening as one path. | The recognizer excludes both quote characters inside either quoted form. Wrapped text remains reachable by scrolling. Preserve complete causes and literal input parsing. [Path recognizer](../snug.py#L706), [wrapping](../snug.py#L738). |
| Explicit closed-symlink-directory label in the source picker. | The source picker refuses entry and uses a generic `@` marker. The single-archive picker has a separate explanatory notice. [Source labels](../snug.py#L855), [archive notice](../snug.py#L1222). |

## Scripted use

| Item | Evidence and boundary |
|---|---|
| Internal non-TTY limits helper applies edits on b; the TTY screen discards them. | Public no-argument startup rejects non-TTY streams. This discrepancy is not an exposed piped menu. [Internal helper](../snug.py#L2551), [public startup](../snug.py#L2864); `tests/test_tui_cancel.py::test_limits_b_and_other_cancel_keys_restore_all_opening_values`. |

## Tiny-file performance

No optimization was implemented. The [full profile report](profile-1.10c2-tiny-files.md) records frozen commit `4f77a2b`, cumulative and exclusive costs, and safety boundaries.

On macOS arm64 with Python 3.14.0, 100,000 one-byte files took 8.120 seconds to create as native TAR and 32.461 seconds to extract with Snug. Plain Python writing took 5.609 seconds. System tar extracted the same archive in 6.689 seconds. Names and payload bytes were verified outside timing. These were single runs with existing caches. The comparators do not establish an equivalent safety contract.

cProfile increased creation to 12.608 seconds and extraction to 52.798 seconds. The report proposes reusing immutable cleaned names, removing duplicate target preparation, reducing quiet progress calls, avoiding cleanup of committed outputs, and reducing repeated source probes. The component ceilings overlap. They are not achieved gains or predictions of unprofiled savings. Each proposal needs a benchmark and safety checks before approval.

Preserve per-file transactions, fsync, staging, publication order and fresh path containment checks. Removing those guarantees for speed is not proposed.

## Unverified

| Area | Verification limit |
|---|---|
| Linux beyond CI | Current Linux execution has not been checked locally. CI configuration alone does not prove a run passed. |
| GitHub CI after the Python patch fixes | All seven jobs in [run 38387356834](https://github.com/yklucz/snug/actions/runs/38387356834) failed on the earlier code. The Linux arm64 runner was available and ran its tests. The D.3 fixes need a new green run on every configured job. Local macOS arm64 results do not verify either Linux architecture. [Workflow](../.github/workflows/tests.yml#L22). |
| TAR member enumeration and extended metadata | D.3 preserves standard library `tarfile` enumeration and its metadata buffering. A global-only PAX `size` field changes the reported logical size while member offsets still use the raw header size. That existing behavior needs separate review if it is changed. `tests/test_tar_stream_validation.py::test_global_only_pax_size_keeps_stdlib_raw_header_offsets`. |
| Complete invalid-checksum TAR headers | A complete invalid-checksum header after a valid member can stop standard library enumeration and return the earlier members. A disposable reproduction matched `acf487c` and D.3. This existing tolerance remains outside the truncated-record fix. [Native inspection](../snug_core.py#L1831). |
| Native terminal glyphs | Automated width and PTY checks do not establish physical font rendering, complex emoji width, cursor placement or flicker in Terminal.app and Ghostty. [Width model](../snug.py#L432); `tests/test_tui_progress.py::test_unicode_width_resize_and_throttle`. |
| Physical disk-full and durability behavior | No disk-filling experiment was run. fsync remains best effort and suppresses OSError. This is a verification limit, not a demonstrated data-loss defect. [Sealing](../snug_core.py#L1284); `tests/test_transactional.py::test_best_effort_fsync_failure_does_not_discard_valid_payload`. |
| Post-release updater path | The real v1.9.0 to v1.10.0 release update cannot be checked until the release exists. Offline tests do not establish live download, installation or rollback behavior. See [Updates](updates.md). |
