"""Installer checks stay offline; real codec smoke tests use installed backends."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

import snug_runtime as runtime

ROOT = Path(__file__).resolve().parents[1]


def make_wheel(path, files):
    with zipfile.ZipFile(path, 'w') as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def test_binding_repair_is_verified_and_reused(tmp_path, monkeypatch):
    wheel = tmp_path / 'binding.whl'
    make_wheel(wheel, {'libarchive/__init__.py': 'binding', 'binding.dist-info/licenses/LICENSE': 'license'})
    artifact = {'url': 'https://example.test/binding', 'sha256': hashlib.sha256(wheel.read_bytes()).hexdigest()}
    (tmp_path / 'runtime-lock.json').write_text(json.dumps({'binding': artifact}))
    calls = []
    def fetch(request, destination):
        assert request == artifact
        calls.append(destination)
        shutil.copyfile(wheel, destination)
    monkeypatch.setattr(runtime, 'download', fetch)
    runtime.ensure_binding(tmp_path)
    runtime.ensure_binding(tmp_path)
    assert len(calls) == 1
    assert runtime.binding_valid(tmp_path)
    assert (tmp_path / 'vendor/binding.dist-info/licenses/LICENSE').read_text() == 'license'
    (tmp_path / 'vendor/libarchive/__init__.py').write_text('broken')
    runtime.ensure_binding(tmp_path)
    assert len(calls) == 2
    assert runtime.binding_valid(tmp_path)
    assert not list(tmp_path.glob('.binding-*'))


def test_failed_download_keeps_existing_files_and_cleans_staging(tmp_path, monkeypatch):
    (tmp_path / 'runtime-lock.json').write_text('{"binding": {}}')
    vendor = tmp_path / 'vendor'
    vendor.mkdir()
    (vendor / 'old.py').write_text('previous install')
    (vendor / runtime.FILE_RECORD).write_text('bad JSON')
    def offline(*args):
        raise OSError('offline')
    monkeypatch.setattr(runtime, 'download', offline)
    with pytest.raises(OSError, match='offline'):
        runtime.ensure_binding(tmp_path)
    assert (vendor / 'old.py').read_text() == 'previous install'
    assert not list(tmp_path.glob('.binding-*'))


def test_download_rejects_tampering(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.urllib.request, 'urlopen', lambda *args, **kwargs: io.BytesIO(b'changed'))
    with pytest.raises(RuntimeError, match='checksum'):
        runtime.download({'url': 'https://example.test/artifact', 'sha256': '0' * 64}, tmp_path / 'download')
    with pytest.raises(RuntimeError, match='HTTPS'):
        runtime.download({'url': 'http://example.test/artifact'}, tmp_path / 'download')


@pytest.mark.parametrize('minor', range(10, 15))
def test_locked_windows_wheels_cover_supported_pythons(minor):
    lock = runtime.load_lock()['windows']
    for name, package in lock['packages'].items():
        if name == 'backports.zstd' and minor == 14:
            continue
        artifact = runtime.select_wheel(package['wheels'], minor)
        assert len(artifact['sha256']) == 64
        assert artifact['url'].startswith('https://files.pythonhosted.org/')
        assert 'cp314t' not in artifact['filename']


@pytest.mark.parametrize('unsafe', ['../outside', '/absolute', 'C:/drive', 'a\\b', 'data/../../escape'])
def test_runtime_wheels_cannot_escape_destination(tmp_path, unsafe):
    wheel = tmp_path / 'unsafe.whl'
    make_wheel(wheel, {unsafe: 'bad'})
    with pytest.raises(RuntimeError, match='unsafe path'):
        runtime.unpack_wheel(wheel, tmp_path / 'output')
    assert not (tmp_path / 'outside').exists()


def test_wheels_keep_runtime_and_licenses_without_test_tools(tmp_path):
    wheel = tmp_path / 'package.whl'
    make_wheel(wheel, {'package/core.py': 'code', 'package/tests/test.py': 'test',
                      'Cryptodome/SelfTest/test.py': 'test', 'package-1.dist-info/licenses/LICENSE': 'license',
                      'package-1.data/scripts/unused.exe': 'tool', 'package-1.data/purelib/extra.py': 'data'})
    runtime.unpack_wheel(wheel, tmp_path / 'packages')
    runtime.record_files(tmp_path / 'packages')
    assert runtime.recorded_files_exist(tmp_path / 'packages')
    assert (tmp_path / 'packages/extra.py').is_file()
    assert (tmp_path / 'packages/package-1.dist-info/licenses/LICENSE').is_file()
    assert not (tmp_path / 'packages/package/tests').exists()
    assert not (tmp_path / 'packages/Cryptodome/SelfTest').exists()
    assert not list((tmp_path / 'packages').rglob('unused.exe'))
    (tmp_path / 'packages/extra.py').unlink()
    assert not runtime.recorded_files_exist(tmp_path / 'packages')


def test_native_package_installs_only_selected_dlls_and_licenses(tmp_path, monkeypatch):
    archive = tmp_path / 'native.tar'
    with tarfile.open(archive, 'w') as tar:
        for name in ['ucrt64/bin/archive.dll', 'ucrt64/bin/unneeded.exe', 'ucrt64/share/licenses/archive/LICENSE']:
            member = tarfile.TarInfo(name)
            member.size = 4
            tar.addfile(member, io.BytesIO(b'data'))
    monkeypatch.setattr(runtime, 'zstd_reader', lambda path: path.open('rb'))
    package = {'name': 'archive', 'dlls': ['ucrt64/bin/archive.dll'], 'licenses': ['ucrt64/share/licenses/archive/LICENSE']}
    runtime.unpack_native(archive, tmp_path / 'native', package)
    assert (tmp_path / 'native/archive.dll').read_bytes() == b'data'
    assert (tmp_path / 'native/licenses/archive/LICENSE').read_bytes() == b'data'
    assert not list((tmp_path / 'native').rglob('*.exe'))
    package['dlls'].append('ucrt64/bin/missing.dll')
    with pytest.raises(RuntimeError, match='incomplete'):
        runtime.unpack_native(archive, tmp_path / 'native', package)


def test_native_package_rejects_link_in_place_of_dll(tmp_path, monkeypatch):
    archive = tmp_path / 'native.tar'
    with tarfile.open(archive, 'w') as tar:
        member = tarfile.TarInfo('ucrt64/bin/archive.dll')
        member.type = tarfile.SYMTYPE
        member.linkname = '/outside'
        tar.addfile(member)
    monkeypatch.setattr(runtime, 'zstd_reader', lambda path: path.open('rb'))
    with pytest.raises(RuntimeError, match='regular file'):
        runtime.unpack_native(archive, tmp_path / 'native', {'dlls': [member.name], 'licenses': []})


def test_storage_does_not_count_shared_symlinks_or_hardlinks_twice(tmp_path, capsys):
    app = tmp_path / 'app'
    app.mkdir()
    data = app / 'data'
    data.write_bytes(bytes(1048576))
    os.link(data, app / 'hardlink')
    (tmp_path / 'outside').write_bytes(bytes(5 * 1048576))
    try:
        (app / 'shared').symlink_to(tmp_path / 'outside')
    except OSError:
        pytest.skip('symlink creation unavailable')
    runtime.storage(app)
    message = capsys.readouterr().err
    assert '1.00 MiB' in message
    assert '5.00 MiB' not in message


@pytest.fixture
def brew_app(tmp_path):
    if os.name == 'nt':
        pytest.skip('Homebrew launcher is Unix-only')
    libarchive = pytest.importorskip('libarchive')
    pytest.importorskip('py7zr')
    app = tmp_path / 'app with spaces'
    app.mkdir()
    for name in ['snug.py', 'snug_core.py', 'snug_ext.py', 'snug_runtime.py', 'runtime.sh', 'runtime-lock.json']:
        shutil.copyfile(ROOT / name, app / name)
    shutil.copytree(Path(libarchive.__file__).parent, app / 'vendor/libarchive', ignore=shutil.ignore_patterns('__pycache__'))
    runtime.record_files(app / 'vendor')
    brew_root = tmp_path / 'brew'
    brew_root.mkdir()
    cellar = brew_root / 'Cellar'
    cellar.mkdir()
    for generation in ['v1', 'v2']:
        python = brew_root / generation / 'libexec/bin/python'
        python.parent.mkdir(parents=True)
        python.write_text('#!/usr/bin/env bash\nif [[ -f "$SNUG_TEST_BROKEN" ]]; then\n  if [[ "$3" == --check && "$4" != libarchive || "$1" == -c || "$2" == -c ]]; then exit 1; fi\nfi\nexec "$SNUG_TEST_PYTHON" "$@"\n')
        python.chmod(0o755)
    native = brew_root / 'native/lib'
    native.mkdir(parents=True)
    suffix = '.dylib' if sys.platform == 'darwin' else '.so'
    library = Path(libarchive.ffi.libarchive_path)
    if not library.is_file():
        candidates = [Path('/opt/homebrew/opt/libarchive/lib/libarchive.dylib'),
                      Path('/usr/local/opt/libarchive/lib/libarchive.dylib'),
                      Path('/usr/lib') / (os.uname().machine + '-linux-gnu') / 'libarchive.so.13']
        library = next((p for p in candidates if p.is_file()), library)
    if not library.is_file():
        pytest.skip('native libarchive path cannot be resolved for launcher fixture')
    (native / ('libarchive' + suffix)).symlink_to(library)
    # The actual managed launcher requires a modern Brew build. Older system
    # libraries remain valid for the source application's existing tests.
    import ctypes
    version = ctypes.CDLL(str(library)).archive_version_number
    if version() < 3008000:
        pytest.skip('managed launcher fixture requires libarchive 3.8+')
    (brew_root / 'opt').mkdir()
    state = brew_root / 'opt/py7zr'
    state.symlink_to(brew_root / 'v1', target_is_directory=True)
    (brew_root / 'opt/libarchive').symlink_to(brew_root / 'native', target_is_directory=True)
    log = tmp_path / 'brew.log'
    brew = tmp_path / 'brew-command'
    brew.write_text('''#!/usr/bin/env bash
set -eu
case "$1" in
  --prefix)
    if [[ "${2:-}" == py7zr ]]; then printf '%s/opt/py7zr\\n' "$SNUG_TEST_BREW_ROOT";
    elif [[ "${2:-}" == libarchive ]]; then printf '%s/native\\n' "$SNUG_TEST_BREW_ROOT";
    else printf '%s\\n' "$SNUG_TEST_BREW_ROOT"; fi;;
  --cellar) printf '%s/Cellar\\n' "$SNUG_TEST_BREW_ROOT";;
  deps) printf 'python@3.14\\n';;
  install|reinstall)
    printf '%s\\n' "$*" >> "$SNUG_TEST_LOG"
    [[ "${SNUG_TEST_OFFLINE:-}" != 1 ]] || exit 1
    if [[ "$1" == reinstall ]]; then rm -f "$SNUG_TEST_BROKEN"; fi;;
  *) exit 1;;
esac
''')
    brew.chmod(0o755)
    env = dict(PATH=os.environ['PATH'], SNUG_BREW=str(brew), SNUG_TEST_BREW_ROOT=str(brew_root),
               SNUG_TEST_LOG=str(log), SNUG_TEST_BROKEN=str(tmp_path / 'broken'),
               SNUG_TEST_PYTHON=sys.executable)
    return app, env, state, log


def launch(app, env, *args):
    return subprocess.run(['bash', str(app / 'runtime.sh'), str(app), '--run', *map(str, args)], env=env,
                          capture_output=True, text=True, timeout=30)


def test_brew_environment_is_reused_and_rediscovered_after_upgrade(brew_app):
    app, env, state, log = brew_app
    assert launch(app, env, '--version').returncode == 0
    state.unlink()
    state.symlink_to(Path(env['SNUG_TEST_BREW_ROOT']) / 'v2', target_is_directory=True)
    shutil.rmtree(Path(env['SNUG_TEST_BREW_ROOT']) / 'v1')
    result = launch(app, env, '--version')
    assert result.returncode == 0, result.stderr
    assert '1.8.0' in result.stdout
    assert not log.exists()
    assert not (app / 'venv').exists()


def test_missing_brew_requests_installation(brew_app, tmp_path):
    app, env, state, log = brew_app
    result = launch(app, dict(env, SNUG_BREW=str(tmp_path / 'missing-brew')), '--version')
    assert result.returncode == 1
    assert 'install Homebrew' in result.stderr
    assert not log.exists()


def test_failed_installer_preserves_previous_app_and_launcher(brew_app, tmp_path):
    app, env, state, log = brew_app
    prefix = tmp_path / 'install with spaces'
    previous = prefix / 'share/snug'
    previous.mkdir(parents=True)
    (previous / 'marker').write_text('previous application')
    launcher = prefix / 'bin/snug'
    launcher.parent.mkdir()
    launcher.write_text('previous launcher')
    env = dict(env, SNUG_TEST_OFFLINE='1', SNUG_PREFIX=str(prefix), SNUG_RAW_BASE=ROOT.as_uri())
    result = subprocess.run(['bash', str(ROOT / 'install.sh')], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert (previous / 'marker').read_text() == 'previous application'
    assert launcher.read_text() == 'previous launcher'
    assert not list((prefix / 'share').glob('.snug-install*'))
    assert not list((prefix / 'bin').glob('.snug-launcher*'))


def test_brew_dependency_repair_and_offline_failure(brew_app):
    app, env, state, log = brew_app
    Path(env['SNUG_TEST_BROKEN']).touch()
    offline = launch(app, dict(env, SNUG_TEST_OFFLINE='1'), '--version')
    assert offline.returncode != 0
    assert 'connection' in offline.stderr
    assert not (app / '.repair-lock').exists()
    repaired = launch(app, env, '--version')
    assert repaired.returncode == 0, repaired.stderr
    assert 'reinstall python@3.14' in log.read_text()
    assert not Path(env['SNUG_TEST_BROKEN']).exists()
    assert not (app / '.repair-lock').exists()


def test_managed_launcher_keeps_password_and_extended_format_workflows(brew_app, tmp_path):
    app, env, state, log = brew_app
    source = tmp_path / 'source with spaces.txt'
    source.write_text('payload')
    secret = tmp_path / 'password.txt'
    secret.write_text('managed-test-secret\n')
    archive = tmp_path / 'encrypted.7z'
    created = launch(app, env, 'create', archive, source, '--password-file', secret, '-q')
    assert created.returncode == 0, created.stderr
    extracted = launch(app, env, 'extract', archive, '-C', tmp_path / 'output', '--password-file', secret, '-q')
    assert extracted.returncode == 0, extracted.stderr
    assert (tmp_path / 'output' / source.name).read_text() == 'payload'
    assert 'managed-test-secret' not in created.stdout + created.stderr + extracted.stdout + extracted.stderr
    fixture = ROOT / 'tests/fixtures/test_read_format_rar.rar'
    result = launch(app, env, 'list', fixture)
    assert result.returncode == 0, result.stderr
