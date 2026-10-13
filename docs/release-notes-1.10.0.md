# Snug 1.10.0 release notes

## Summary

This release improves terminal navigation, result screens, progress, and archive error reporting. It rejects incomplete TAR headers and invalid TAR hardlinks. Incomplete archive creation now has an explicit exit status.

The comparisons below use the released `v1.9.0` tag, commit `7bb6ab6`, as the baseline.

## Supported platforms

Supported: macOS on Apple Silicon and Linux. Intel Macs may work but are not tested. This release ends Intel Mac testing and makes Apple Silicon the macOS priority. Python 3.10 or newer is still required. See [Installation](installation.md#requirements).

CI is configured for Linux x86_64 on Python 3.10 to 3.14, Linux arm64 on Python 3.14, and macOS arm64 on Python 3.14. The Linux arm64 job is new and remains unverified until it runs on GitHub. See [Development](development.md#environment-and-checks).

## Upgrade notes

- Native ZIP and TAR creation could silently omit unreadable inputs under `-q` and exit 0 in v1.9.0. A newly published incomplete archive now warns on stderr, even under `-q`, and exits 5. Warnings identify omitted inputs and their causes. With `-q` and closed stderr, the released v1.9.0 also returned 0; 1.10.0 retains exit 5 after publication. Intentional omissions still succeed. Commits `8f46606` and `0f11e00`; `tests/test_create_skips.py::test_cli_partial_create_warns_even_when_quiet`; `tests/test_cli_create_closed_stderr.py::test_published_incomplete_create_keeps_exit_five_with_closed_stderr`.
- An all-skipped create could publish an empty archive and exit 0 in v1.9.0. It now publishes nothing and exits 2 when errors prevent every input from being included. Commit `8f46606`; `tests/test_create_skips.py::test_cli_all_accidentally_skipped_publishes_nothing`.
- `--force` could replace an existing archive with an incomplete one in v1.9.0. An accidental omission now preserves the existing archive and exits 2. Complete creation retains the existing replacement policy. Commit `8f46606`; `tests/test_create_skips.py::test_cli_forced_partial_preserves_existing_archive`.
- Different files that collide on one archive name now cause an accidental-skip warning and follow those publication rules. v1.9.0 silently deduplicated them. Repeated references to the same real file remain intentional skips. Commit `8f46606`; `tests/test_create_skips.py::test_different_file_name_collision_is_accidental` and `::test_same_real_file_name_collision_remains_intentional`.

Scripts that compare exact stderr text or progress lines may need updates. The current exit table is:

| Status | Meaning |
|---|---|
| `0` | Command completed. Check summaries for skipped entries. |
| `1` | Interactive mode lacks a terminal, a managed dependency check or repair failed, or an error could not be written to closed stderr. |
| `2` | Invalid arguments, archive, format or I/O error, or unhealthy required components reported by doctor. |
| `3` | Extraction or testing rejected an unsafe archive. |
| `4` | An explicit extraction resource limit was exceeded. |
| `5` | An archive was published with inputs omitted because of errors. |
| `130` | CLI interruption. Main-menu and secure-password TUI interruptions can also exit 130. |

See [Usage](usage.md#exit-status) for the complete contract. No-argument, non-TTY startup is unchanged: v1.9.0 already printed help and exited 1. [Startup check](../snug.py#L2864).

## TUI changes

- Keyboard input decodes UTF-8 characters and preserves unrelated bytes after invalid input. Commit `c43390a`; `tests/test_tui_input.py::test_unicode_text_produces_one_event_per_character` and `::test_noncontinuation_byte_after_bad_utf8_is_not_lost`.
- Full and compact layouts reserve space for key hints. Lists follow the selected row, and resizing redraws without another key. Commit `69cd815`; `tests/test_tui_layout.py::test_every_screen_obeys_row_column_and_footer_budgets` and `::test_idle_resize_recovers_picker_without_a_key_and_without_redundant_redraws`.
- The source picker opens directories with Enter, marks paths with Space, and confirms with Tab. Marks survive directory changes and filtering. Filtering normalizes comparisons while preserving original names and paths. Commit `7ca07ec`; `tests/test_tui_picker.py::test_enter_opens_a_directory_instead_of_accepting_it`, `::test_confirm_accepts_exact_sorted_marks_including_hidden_and_other_directories`, and `::test_filter_normalizes_comparison_only_preserving_original_display_and_path`.
- Member selection toggles exact names with Space and confirms marked names with Tab in archive order. Cancelling discards edits. Commit `e4d6ff5`; `tests/test_tui_members.py::test_space_toggles_exact_member_and_keeps_cursor`, `::test_tab_confirms_only_marks_in_archive_order`, and `::test_cancel_discards_every_edit_and_does_not_mutate_prior`.
- Limit changes require Apply. Back, Escape, Ctrl+C, b, q and Q discard the overview draft. Cancelling a field discards only that field. Commits `27dad03` and `4b8fcc1`; `tests/test_tui_limits.py::test_only_explicit_apply_keeps_all_edited_values`; `tests/test_tui_cancel.py::test_limits_b_and_other_cancel_keys_restore_all_opening_values`.
- Line prompts support cursor editing and separate in-memory history by prompt kind. Rejected archive paths return to a prefilled editor. Literal spaces and punctuation are preserved in archive paths. Tab opens the archive browser; cancelling returns to the original draft. Commits `07013c8`, `1d180ca`, `4b8fcc1`, and `ac7f6f1`; `tests/test_tui_editor.py::test_prompt_history_stays_in_memory_and_is_separate_by_kind` and `::test_tty_manual_path_keeps_exact_unicode_spaces_and_literal_punctuation`; `tests/test_tui_browse.py::test_browser_cancel_returns_to_exact_prefilled_prompt`.
- Ctrl+C cancels the current option, member, limit or line prompt. It cancels either picker immediately. At the main menu it still quits with 130. Secure password prompts retain their separate interruption behavior. Commit `f36738c`; `tests/test_tui_interrupt.py::test_nested_screens_apply_the_same_escape_effects_without_exiting_session`, `::test_main_menu_ctrl_c_still_quits_with_130`, and `::test_secure_getpass_and_tty_line_prompt_keep_separate_interrupt_contracts`.
- Create, extract and test results put status and counts before details. Result and error pauses wrap text and shorten long paths in the middle. Overflow has a hidden-row count. Up, Down, Page Up, Page Down, Home, and End reach later rows. Enter, Escape, and Ctrl+C dismiss the pause. Commit `fcc5aca`; `tests/test_tui_results.py::test_many_create_skips_keep_status_counts_and_normal_summary_first`, `::test_long_unicode_error_wraps_cause_and_retains_quoted_path_tail`, and `::test_overflow_position_and_every_scroll_key_keep_all_rows_reachable`.
- Ctrl+C during TUI create, extract or test now shows a cancellation pause and returns to the menu. The pause describes any published output that remains. Extraction retains published files and removes unfinished staging where possible. CLI interruption still exits 130. Commit `fcc5aca`; `tests/test_tui_operation_cancel.py::test_cancelled_real_engine_handler_returns_to_live_main_menu`, `::test_extract_cancel_retains_only_published_files_and_cleans_staging`, and `::test_cli_keeps_exit_130_on_operation_interrupt`.
- TUI progress uses the CLI completion rules and fits the screen by cell width. Unknown totals remain indeterminate until success. Skipped bytes are not counted as processed. Commit `fcc5aca`; `tests/test_tui_progress.py::test_unknown_totals_stay_working_until_explicit_completion`, `::test_skipped_work_finishes_without_fabricating_bytes`, and `::test_unicode_width_resize_and_throttle`.
- Long archive paths on older Python versions now show a rejection message and a prefilled editor instead of raising an error. Commit `4cbff3e`; [input probes](../snug.py#L815); `tests/test_path_probes.py::test_manual_file_probe_rejects_and_reopens_exact_draft`.

## CLI changes

- Common corruption, password, permission, format and limit errors identify the archive and affected member when available. They give a plain cause and recovery advice where implemented. Ordinary error codes are unchanged. Creation omissions advise checking permissions or excluding unreadable inputs. Directory conflicts suggest an empty destination or renaming or removing the conflicting directory. Unsafe hardlink errors name the originating member and target. Commits `0f299ef` and `1772ab6`; [error formatting](../snug.py#L3403), [recovery advice](../snug.py#L3361); `tests/test_cli_error_context.py` and `tests/test_c2_error_advice.py`.
- An early-closing stdout consumer could produce exit 120 and a Python shutdown diagnostic in v1.9.0. `list`, `info`, `formats`, and `doctor` now exit 0 silently. A consumer can receive only part of a JSON report. Schema version 1 keys and types are unchanged. Commit `0f299ef`; `tests/test_cli_closed_pipes.py::test_real_report_pipe_closure_is_silent_success` and `::test_complete_json_report_schema_is_unchanged`.
- A closed stderr progress pipe could fail create, extract or test in v1.9.0. Progress now disables itself and preserves the operation's normal result. Active terminal progress ends before an error message. Commit `0f299ef`; `tests/test_cli_progress_cleanup.py::test_real_cli_closed_stderr_preserves_success_and_disk_effects` and `::test_cli_failure_terminates_only_an_active_tty_progress_line`.
- A real error written to closed stderr returned 120 in the buffered v1.9.0 reproduction. It returns 1 now. Diagnostic argument and operational errors still use plain stderr with empty stdout, rather than a JSON error object. Commit `0f299ef`; `tests/test_cli_closed_pipes.py::test_closed_error_pipe_preserves_existing_failure_status`, `::test_diagnostic_operational_errors_keep_plain_stderr_and_empty_stdout`, and `::test_diagnostic_argument_errors_keep_empty_stdout`.
- Unknown-size CLI streams stay indeterminate until success and show processed bytes. v1.9.0 could show live completion early. Item-only creation retains its entry counter without inventing a byte rate. Successful skipped extraction reaches completion without adding skipped bytes to the count. Non-TTY final progress adds `, N skipped` when nonzero. Terminal progress uses stderr's width, drops optional fields when space is short, and shortens names by cell width. Commit `0f11e00`; `tests/test_cli_progress_layout.py`.

## Fixes

- v1.9.0 accepted truncated TAR headers and could report success while omitting a member. `test`, `list`, `info`, and `extract` now reject incomplete nonzero headers and incomplete PAX or GNU metadata with exit 2. Extraction fails before destination creation. Complete members without zero end blocks, and archives with zero padding, remain accepted. Compressed TARs use the same checks. Commit `2f11601`; [header checks](../snug_core.py#L1471); `tests/test_tar_headers.py`.
- v1.9.0 could report `Archive is OK.` for a dangling TAR hardlink. `test` and `extract` now reject a missing or later target with exit 2 before destination creation. `list` and `info` remain available for diagnosis. [Hardlink checks](../snug_core.py#L1083); `tests/test_tar_hardlinks.py`.
- A selected TAR hardlink whose target is excluded or removed by `--strip-components` is skipped with a stderr warning naming both paths. The warning is visible under `-q`; exit remains 0. [Selection warnings](../snug_core.py#L1095), [warning output](../snug.py#L3558); `tests/test_tar_hardlinks.py`.
- An unreadable ZIP reports a permission error and advice even without libarchive. v1.9.0 could report an unsupported format. [ZIP probing](../snug_core.py#L1523); `tests/test_zip_permissions.py`.
- Ctrl+C during create setup no longer leaves the staging file created by that run. Existing archives remain intact. Commit `4cbff3e`; [staging cleanup](../snug_core.py#L2191); `tests/test_create_staging.py::test_sigint_immediately_after_exclusive_creation_cleans_staging` and `::test_setup_cleanup_preserves_a_replacement_staging_inode`.

## Known limitations

Optional backends determine format, codec and encryption availability. See [Supported formats](supported-formats.md). Extraction is transactional per regular file, with no whole-archive rollback. [Publication contract](usage.md#extract-archives).

A second Ctrl+C during TUI engine cleanup is ignored until cleanup finishes. Complex emoji glyph widths still depend on the terminal and font. Current Linux behavior beyond CI, Python 3.10 to 3.12 locally, physical disk-full behavior, and the live post-release updater path remain unverified. See the [remaining backlog](backlog-1.10c.md) and [tiny-file profile](profile-1.10c2-tiny-files.md). No performance optimization was implemented.
