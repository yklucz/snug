from pathlib import Path
import shutil

import snug


def test_picker_detects_real_new_and_renamed_archives(fixture_dir, tmp_path):
    for source, name in [
        ('test_read_format_rar.rar', 'sample.rar'),
        ('test_read_format_cab_1.cab', 'sample.cab'),
        ('test_read_format_iso_2.iso', 'sample.iso'),
        ('test_read_format_7zip_lzma1_2.7z', 'renamed.bin'),
    ]:
        shutil.copyfile(fixture_dir / source, tmp_path / name)
    (tmp_path / 'plain.txt').write_text('plain text')
    assert {p.name for p in snug._find_archives(tmp_path)} == {
        'sample.rar', 'sample.cab', 'sample.iso', 'renamed.bin',
    }


def test_archive_picker_escapes_names_and_accepts_manual_path_key():
    options = snug._build_archive_options([Path('sample\x1b[31m.rar')])
    assert '\x1b' not in options[0][1]
    screen = snug._MenuScreen('Choose an archive', None, options)
    assert screen.handle('m') is snug._EXIT
    assert screen.result == 'm'
