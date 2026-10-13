# Pending 1.10C backlog

Audited at `fcc5aca`, with the 1.10B.8 closure fixes noted below. These are candidates and verification limits, not implementation approval. Documented choices are identified separately from defects.

## Priority follow-ups

| Item | Class | Evidence and boundary |
|---|---|---|
| Second Ctrl+C cannot escape hung TUI engine cleanup. High priority. | usability | The first SIGINT installs `SIG_IGN` until the operation scope unwinds. A second Ctrl+C is ignored during that interval. Cleanup protection is deliberate, but a hung cleanup has no in-app escape. [snug.py:2202](../snug.py#L2202), [snug.py:2210](../snug.py#L2210); `tests/test_tui_operation_cancel.py::test_second_ctrl_c_is_ignored_during_engine_cleanup` and `::test_second_real_sigint_cannot_interrupt_engine_staging_cleanup`. Preserve safe cleanup and signal restoration when reviewing alternatives. |
| TAR hardlinks with missing targets can pass testing and be skipped during extraction. High priority. | usability | `snug test missing-target.tar` exits 0 and prints `Archive is OK.` for a hardlink whose target is absent. `snug extract missing-target.tar -C out` exits 0 and reports the link as skipped. Testing excludes hardlinks from payload checks, [snug_core.py:2253](../snug_core.py#L2253); extraction silently skips an unresolved target, [snug_core.py:1346](../snug_core.py#L1346); success wording is at [snug.py:3434](../snug.py#L3434). Design question: when member selection excludes a hardlink's target, should extraction include that target, skip with a reason, or reject the selection? Separate that decision from an archive whose target is missing entirely. Preserve member selection, validation, and exit statuses until a change is approved. |

## Deferred progress and error work

| Item | Class | Evidence and boundary |
|---|---|---|
| Tiny-input summaries show ratios such as 6725.0%. | cosmetic | The shared create summary reports total container bytes divided by input bytes. Container overhead can exceed the input size. The TUI uses that same summary. [snug_core.py:1017](../snug_core.py#L1017), [snug.py:3408](../snug.py#L3408), [snug.py:2135](../snug.py#L2135). Review wording without changing the measured byte counts. |
| SIGINT during active CLI progress can leave an extra blank line. | cosmetic | Progress abort writes a newline and the interruption message begins with another newline. [snug.py:3137](../snug.py#L3137), [snug.py:45](../snug.py#L45), [snug.py:4111](../snug.py#L4111); `tests/test_cli_progress_cleanup.py::test_cli_failure_terminates_only_an_active_tty_progress_line`. Keep error separation and quiet-mode behavior. |
| CLI and TUI have separate Unicode cell-width helpers. | cosmetic | [snug_core.py:107](../snug_core.py#L107), [snug_core.py:113](../snug_core.py#L113), [snug.py:424](../snug.py#L424), [snug.py:432](../snug.py#L432). Consider sharing the width model without changing rendering contracts. This does not establish native emoji widths. |
| Optional 7z creation has long gaps without activity updates. | usability | py7zr exposes no input-byte callbacks here. Snug updates an item counter before each blocking write, with no heartbeat during the write. [snug_ext.py:762](../snug_ext.py#L762), [snug_ext.py:769](../snug_ext.py#L769). The counter is now honest; do not fabricate bytes or ETA. |
| Update download activity is not displayed. | usability | The download loop has no progress callback or renderer. [snug_update.py:447](../snug_update.py#L447). Live network update, download, and cancellation remain unverified. Keep installation and verification policy separate from presentation. |
| Generic TUI errors still use backend wording. | usability | Result pauses now wrap and scroll, but ordinary errors still print `_safe(exc)` rather than the CLI contextual formatter. [snug.py:2811](../snug.py#L2811), [snug.py:2816](../snug.py#L2816), [snug.py:3388](../snug.py#L3388). Archive context, backend internals, and recovery advice need their own review. |
| Some CLI failures and partial-create warnings omit an obvious next step. | usability | Existing destination directories report that a file cannot replace a directory without suggesting another destination or renaming the conflict. All-unreadable create, forced partial replacement, and successful incomplete creation give permission causes and publication facts without a corrective action. [snug_core.py:1176](../snug_core.py#L1176), [snug_core.py:2314](../snug_core.py#L2314), [snug.py:3365](../snug.py#L3365), [snug.py:3385](../snug.py#L3385), [snug.py:3508](../snug.py#L3508). The B.7 failure matrix retains these as wording gaps. |
| Unsafe hardlink diagnostics can name the target without the originating link member. | usability | Hardlink validation resolves and checks the target before payload progress supplies member context. [snug_core.py:1121](../snug_core.py#L1121), [snug_core.py:1340](../snug_core.py#L1340). Add originating-member context only within a separately approved change; preserve path validation. |
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

## Verification work

| Item | Class | Limit |
|---|---|---|
| Current Linux and other supported Python versions. | usability | macOS Python 3.14.0 passes 2,480 tests without skips after 1.10B.8. Local Python 3.13.5 passes the unchanged long-path cases and all 44 new checks, but its full run has one pre-existing permission-message failure and 181 skips. Python 3.13.16 lacks pytest. No local 3.10, 3.11, or 3.12 interpreter was found. Current Linux execution remains unverified. |
| Linux arm64 CI. | usability | Linux arm64 CI is unverified until it runs on GitHub; arm64 hosted runners may be unavailable for private repositories. [New workflow job](../.github/workflows/tests.yml#L22). |
| Native Terminal.app and Ghostty glyph, cursor, wrapping, and flicker checks. | cosmetic | Automated width and PTY byte checks do not establish physical font rendering or complex emoji width. [snug.py:424](../snug.py#L424); `tests/test_tui_progress.py::test_unicode_width_resize_and_throttle`. |
| Physical disk-full and durability behavior. | safety | No disk-filling experiment was run. fsync remains best effort and suppresses OSError. [snug_core.py:1216](../snug_core.py#L1216); `tests/test_transactional.py::test_best_effort_fsync_failure_does_not_discard_valid_payload`. This is a verification limit, not a demonstrated data-loss defect. |
