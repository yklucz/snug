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


## 1.10D.6 pre-release review

This review starts from `34f24e91e6a3d3a7511912b7d4c6a55e2b2f885c`. Findings below record D.6 and its D.7 follow-up. The release remains paused. The retained D.5 artifacts belong to the original commit and were left unchanged. A new release commit requires fresh sign-off and rebuilt artifacts.

### Resolved SonarLint S3776 findings

D.6 deferred these four cognitive complexity findings. The user requested the refactor separately after the D.6 review. Commit `ca37c85` resolved all four with focused helper extractions and an early return. Its commit message does not mention the complexity refactor.

The installed SonarPython 5.32.0.37250 analyzer reproduced all four baseline scores. It measured the refactored scores below. All meet the threshold of 15. These measurements used a direct offline invocation of its parser and cognitive-complexity visitor.

| Function and current location | Before | After | Behavior retained |
|---|---:|---:|---|
| [`_iter_items`](../snug_core.py#L998) | 29 | 15 | Duplicate identity/reporting moved to `_skip_duplicate_item`; source order, cancellation and collision policy remain unchanged. |
| [`NativeBackend._add_to_tar`](../snug_core.py#L1952) | 18 | 10 | Regular payload handling moved to `_tar_add_regular_file`; inode cleanup, progress and counters retain their ordering. |
| [`ArchiveEngine.create`](../snug_core.py#L2694) | 23 | 12 | Publication moved to `_publish_created_archive`; staging, exclusive partial publication, preservation and exception boundaries remain unchanged. |
| [`_cli_error_text`](../snug.py#L3426) | 17 | 13 | An early return for create-policy errors removes nesting without changing error precedence, context or exit status. |

The new helper `_skip_duplicate_item` scores 6. `NativeBackend._tar_add_regular_file` scores 7. `ArchiveEngine._publish_created_archive` scores 11. Every existing function outside those four has the same AST as the preserved D.6 source. Existing creation, staging, inode-cache and CLI error regressions cover the refactors. No new tests or behavior changes were introduced for this follow-up.

### CodeQL alerts

The latest Python analysis was `1964934367`, CodeQL 2.27.2, for the original commit. Its SARIF was fetched with `Accept: application/sarif+json`. Both alerts remain open and unchanged. Both use `py/clear-text-logging-sensitive-data`, with rule severity error and security severity high. Their message is: "This expression logs sensitive data (password) as clear text."

| Alert | Analyzed source and sink | Verdict |
|---|---|---|
| [1](https://github.com/yklucz/snug/security/code-scanning/1) | `_menu_password_label(...)` at original `snug.py:1992`, `2614`, `2720`, through menu tuples/lists to the plain-menu print at `626`. SARIF has four paths, including two create variants. | False positive. The [helper](../snug.py#L1979) returns fixed set/none/unavailable labels. It checks presence, without formatting the password value. |
| [2](https://github.com/yklucz/snug/security/code-scanning/2) | `_cli_password_error(chain)` and `password_error` at original `snug.py:3392`, through fixed advice, context and `_safe` to stderr at `4084`. | False positive. The [helper](../snug.py#L3336) returns fixed recovery strings or None, rather than the exception or password value. |

The analyzed query revision is `a7057b836bf29767461026718b14d20790b9085f`. Its [sensitive-function model](https://github.com/github/codeql/blob/a7057b836bf29767461026718b14d20790b9085f/python/ql/lib/semmle/python/dataflow/new/SensitiveDataSources.qll#L66-L111) classifies call results by function name. Its [name heuristic](https://github.com/github/codeql/blob/a7057b836bf29767461026718b14d20790b9085f/shared/concepts/codeql/concepts/internal/SensitiveDataHeuristics.qll#L76-L79) matches password in those names. The local assignment can also be a modeled source. This model does not establish that the returned text contains a secret.

Suggested dismissal comment for alert 1:

> The reported source is `_menu_password_label(...)`, selected by CodeQL's sensitive-function-name heuristic. The helper returns only fixed set/none/unavailable status labels and uses the password solely in an is-not-None check. All reported create, extract and test paths carry those labels to `_select_menu_plain`, without the secret value. Secure prompt/file and backend-error canary checks confirm these paths do not print the password.

Suggested dismissal comment for alert 2:

> The reported source is `_cli_password_error(chain)`, assigned to `password_error`. Those names trigger CodeQL's password-name heuristic. The helper returns fixed recovery advice or None, never a password or raw exception. The reported sink prints that advice with archive/member context. ZIP/7z wrong/missing-password, prompt/file and closed-stderr canary checks found no secret on this path.

No dismissal was submitted. An optional cosmetic change would rename the two helpers to `_menu_credential_label` and `_cli_credential_error`, and the local to `credential_error`. That would touch eight source lines. It was not implemented; another analysis would be needed to verify its effect. Passing a boolean alone would leave the sensitive function-name heuristic in place.

Before the fix, 270 real CLI, secure-prompt, closed-stderr and forced-libarchive cases across local Python 3.14.0 and downloaded 3.14.8 printed no canary password. Wrong/missing/correct passwords, password files, actual `--password` prompts, create/extract/test menus, encrypted ZIP, and data/header-encrypted 7z were covered. Forced libarchive probes also reported its unsupported 7z AES cases rather than claiming support.

A separate redaction defense gap was reproduced by injecting a backend exception containing an escaped `repr(password)`. Literal-only replacement missed that form in the real optional-backend-to-TUI reporting chain. This was outside both SARIF paths. No tested current zipfile, py7zr 1.1.4 or libarchive-c 5.3 wrong/missing-password message was observed to contain it. The [optional-backend formatter](../snug_ext.py#L58) and [TUI guard](../snug.py#L1910) now redact unique literal, repr-body and ASCII-escaped-body variants. Forty-two [regression cases](../tests/test_password_redaction.py) produced 28 failures and 14 passes before the fix, then all passed. The final two canary matrices covered 224 cases, including 42 real secure prompts and 44 injected exception cases, with no password output or wrong statuses.

### Quoted-path display performance

The original `_TUI_QUOTED_PATH` at original `snug.py:706` was:

```python
_TUI_QUOTED_PATH = re.compile(
    r"(['\"])((?![A-Za-z][A-Za-z0-9+.-]*://)[^'\"]*[\\/][^'\"]*)\1")
```

It recognizes same-quoted text containing a slash or backslash, excluding an initial ASCII URL scheme. Its only consumer was `_tui_pause_paths`, which shortened the matched path with a middle ellipsis. The overlapping nonquote repetitions admit separators on both sides. A missing or mismatched closing quote makes the engine retry quadratically many partitions. A 4x input increase produced about 15-17x more time. Realistic 64k inputs exceeded the timed operation's three-second limit on both downloaded interpreters. This met the D.6 fix rule.

Untrusted ZIP names enter through [`_zip_entry`](../snug_core.py#L2223); TAR/PAX names and link targets enter through [`_tar_entry`](../snug_core.py#L2237). Extraction [inspects and validates](../snug_core.py#L2767) these entries. [Member validation](../snug_core.py#L704) and [structure checks](../snug_core.py#L1193) can put their repr into errors; names and link targets also appear in list output through [`_format_entry_verbose`](../snug.py#L3492). Backend exceptions retain printable path text through [`_error_text`](../snug_ext.py#L58). The [TUI handler](../snug.py#L2821) captures these errors, the [result collector](../snug.py#L808) retains printed lines, and [`_wait_for_enter`](../snug.py#L824) displays them through [`_PauseScreen`](../snug.py#L761), [`_tui_pause_lines`](../snug.py#L752) and [`_tui_pause_paths`](../snug.py#L732). Create skip reasons, cleanup warnings and ordinary result text can take the same route. The dedicated result-path branches at `_tui_pause_paths` bypass quoted recognition. Non-TTY pauses return without rendering. CLI and JSON reports do not call this recognizer.

The replacement [`_tui_quoted_paths`](../snug.py#L711) examines adjacent quote boundaries once. It preserves the same quote, separator and URL rules, failed-quote overlap behavior, cell-width shortening and Unicode normalization. It works on Python 3.10. No archive-name validation, traversal protection or extraction policy changed.

The new [performance and compatibility tests](../tests/test_tui_path_performance.py) produced two timeout failures and 17 passes on the original code. With the fix, those 19 cases and the 65 unchanged result-screen tests passed. A disposable differential check also compared 375,923 old/new outputs, including mixed quotes, URLs, wide/combining text and four widths, with no difference.

ZIP filenames have a 65,535-byte field ceiling. The real ZIP probe used exactly that length. PAX has length-prefixed names and no universal 64k name ceiling; real 64k, 256k and 1M-character PAX names were tested. Each probe verified the actual member name, caused the existing engine to reject traversal, and passed the real diagnostic to the renderer. This did not require unsafe extraction.

Timings below are single observations on macOS arm64, in milliseconds, before / after. Imports, input construction and archive generation completed before a READY marker. A killable child then received three seconds for the timed `_tui_pause_paths` operation. `>3000 timeout` is a lower bound, not an extrapolated completion time. The tables include ordinary text, no-separator text, repeated slash/backslash, mismatched/end-failing text, wide plus combining characters, valid quoted paths, repr-style member diagnostics and URL controls. `1k` means 1,000 characters.

Python 3.10.22; milliseconds; before / after

| Family | 1k | 4k | 16k | 64k | 256k | 1M |
|---|---:|---:|---:|---:|---:|---:|
| plain_unquoted | 0.030 / 0.027 | 0.062 / 0.060 | 0.191 / 0.190 | 0.729 / 0.715 | 3.169 / 2.791 | 11.464 / 11.112 |
| plain_unclosed | 0.037 / 0.027 | 0.103 / 0.061 | 0.373 / 0.194 | 1.365 / 0.711 | 5.595 / 2.793 | 21.795 / 10.853 |
| separators_unclosed | 4.069 / 0.025 | 60.171 / 0.063 | 1029.487 / 0.191 | >3000 timeout / 0.710 | >3000 timeout / 2.792 | >3000 timeout / 10.900 |
| backslash_unclosed | 4.318 / 0.027 | 62.928 / 0.063 | 983.925 / 0.193 | >3000 timeout / 0.760 | >3000 timeout / 2.779 | >3000 timeout / 11.158 |
| separators_wrong_quote | 4.052 / 0.029 | 68.946 / 0.064 | 1053.462 / 0.193 | >3000 timeout / 0.714 | >3000 timeout / 2.801 | >3000 timeout / 10.908 |
| end_failure | 2.076 / 0.030 | 32.935 / 0.062 | 547.875 / 0.198 | >3000 timeout / 0.728 | >3000 timeout / 2.799 | >3000 timeout / 10.885 |
| unicode_unclosed | 0.961 / 0.038 | 16.243 / 0.090 | 252.615 / 0.249 | >3000 timeout / 0.946 | >3000 timeout / 3.669 | >3000 timeout / 14.342 |
| quoted_path | 0.489 / 0.513 | 1.374 / 1.353 | 4.716 / 4.749 | 18.178 / 18.766 | 72.098 / 74.349 | 279.646 / 291.370 |
| repr_member | 4.084 / 0.029 | 62.648 / 0.066 | 1007.861 / 0.188 | >3000 timeout / 0.725 | >3000 timeout / 3.003 | >3000 timeout / 10.993 |
| quoted_url | 0.026 / 0.029 | 0.059 / 0.062 | 0.192 / 0.203 | 0.713 / 0.711 | 2.777 / 2.795 | 10.856 / 11.640 |

| Real archive diagnostic | before ms | after ms |
|---|---:|---:|
| zip_max_name 65535 | >3000 timeout | 0.720 |
| pax_member 64000 | >3000 timeout | 0.701 |
| pax_member 256000 | >3000 timeout | 2.765 |
| pax_member 1000000 | >3000 timeout | 10.853 |

Python 3.14.8; milliseconds; before / after

| Family | 1k | 4k | 16k | 64k | 256k | 1M |
|---|---:|---:|---:|---:|---:|---:|
| plain_unquoted | 0.030 / 0.031 | 0.065 / 0.062 | 0.207 / 0.193 | 0.738 / 0.817 | 2.858 / 2.965 | 10.761 / 11.061 |
| plain_unclosed | 0.038 / 0.030 | 0.101 / 0.071 | 0.349 / 0.194 | 1.307 / 0.775 | 5.516 / 2.828 | 20.253 / 11.634 |
| separators_unclosed | 4.022 / 0.031 | 55.930 / 0.063 | 922.441 / 0.218 | >3000 timeout / 0.821 | >3000 timeout / 2.747 | >3000 timeout / 11.930 |
| backslash_unclosed | 3.735 / 0.031 | 57.078 / 0.065 | 1004.809 / 0.207 | >3000 timeout / 0.826 | >3000 timeout / 3.139 | >3000 timeout / 12.848 |
| separators_wrong_quote | 5.712 / 0.031 | 80.266 / 0.071 | 1177.108 / 0.231 | >3000 timeout / 0.872 | >3000 timeout / 3.388 | >3000 timeout / 11.872 |
| end_failure | 1.890 / 0.030 | 28.984 / 0.064 | 449.678 / 0.228 | >3000 timeout / 0.909 | >3000 timeout / 3.619 | >3000 timeout / 12.404 |
| unicode_unclosed | 1.185 / 0.033 | 20.613 / 0.080 | 280.403 / 0.297 | >3000 timeout / 1.016 | >3000 timeout / 3.926 | >3000 timeout / 15.362 |
| quoted_path | 1.525 / 0.438 | 1.046 / 1.231 | 3.656 / 4.595 | 14.741 / 16.966 | 60.275 / 76.561 | 245.167 / 279.102 |
| repr_member | 3.895 / 0.040 | 57.499 / 0.078 | 922.853 / 0.280 | >3000 timeout / 0.822 | >3000 timeout / 3.277 | >3000 timeout / 12.589 |
| quoted_url | 0.027 / 0.036 | 0.063 / 0.065 | 0.217 / 0.286 | 0.776 / 0.907 | 2.773 / 3.526 | 11.714 / 12.157 |

| Real archive diagnostic | before ms | after ms |
|---|---:|---:|
| zip_max_name 65535 | >3000 timeout | 0.724 |
| pax_member 64000 | >3000 timeout | 0.711 |
| pax_member 256000 | >3000 timeout | 2.930 |
| pax_member 1000000 | >3000 timeout | 11.909 |


Failure families now scale approximately linearly: 64k to 1M is about 15.6x more input and about 15x more time. At 1M, the slowest failing family took 14.342ms on 3.10.22 and 15.362ms on 3.14.8. Valid huge Unicode paths also retain existing cell/cluster work, reaching 291.370ms and 279.102ms respectively. All after cases completed. These observations establish the regression fix, rather than a general archive-performance claim.

Real PTYs displayed a long NFD/wide path and the actual maximum-length ZIP diagnostic at 80x24 and 40x10. Both pauses exited normally with exact equality of the complete saved termios attributes. First frames took 0.033-0.056 seconds; complete two-pause runs took 0.256-0.548 seconds on downloaded 3.10.22 and 3.14.8. These checks do not establish physical font or emoji rendering.

The same-class regex audit found no second overlapping-repeat/failing-suffix construction in the four requested modules:

| Pattern and location | Reachability and boundary |
|---|---|
| [`_TUI_SGR`](../snug.py#L420), [`_ANSI_RE`](../snug_core.py#L48) | Untrusted display text. A single numeric/semicolon run excludes the next ESC candidate. |
| [`_CONTROL_RE`](../snug_core.py#L49) | Untrusted names and errors through `_safe`; one-character class. |
| [`_TUI_QUOTE`, `_TUI_URL_SCHEME`, `_TUI_BARE_PATH`](../snug.py#L706) | Untrusted result text. Single quote characters, a start-anchored scheme, or a fixed prefix with one successful run. |
| [`parse_size`](../snug_core.py#L250) | User size arguments, capped at 128 characters before matching. |
| [`_DRIVE_RE`](../snug_core.py#L386), [Windows device-name fullmatch](../snug_core.py#L728) | Untrusted archive names; fixed prefix or fixed-length device name. |
| [Digest fullmatch](../snug.py#L3649), [diagnostic name fullmatch](../snug.py#L3702), [version prefix](../snug.py#L3741) | Doctor/runtime metadata; fixed length, bounded name, or start-anchored dot-delimited digits. |
| [`_VERSION`](../snug_update.py#L52), [release digest](../snug_update.py#L435) | Local/remote update metadata. Versions are capped at 128 characters; digest has exactly 64 hex characters. |
| `snug_ext.py` | No regex calls. |

The two requested style fixes combine the native TAR inspection context managers at [`NativeBackend.inspect`](../snug_core.py#L1838) and replace the inode-list snapshot with a dictionary snapshot at [`NativeBackend._add_to_tar`](../snug_core.py#L1984). Snapshot semantics remain necessary because the loop deletes from the original dictionary. Existing TAR create/skip/header/stream tests cover these paths. The separately requested complexity refactors are recorded above.
