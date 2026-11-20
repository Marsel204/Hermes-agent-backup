#!/usr/bin/env python3
"""Create an encrypted, versioned Hermes setup snapshot; push pending commits."""
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone

import yaml

HOME = Path.home()
REPO = Path(__file__).resolve().parent
HERMES = HOME / '.hermes'
SOURCE = HERMES / 'hermes-agent'
STATE = HOME / '.local/state/hermes-backup'
KEY = HOME / '.config/hermes-backup/recovery-key.txt'
SENSITIVE = re.compile(r'api[_-]?key|token|secret|password|authorization|credential', re.I)


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def clean_config(value):
    if isinstance(value, dict):
        return {k: ('REENTER_AFTER_RESTORE' if SENSITIVE.search(str(k)) and v else clean_config(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [clean_config(v) for v in value]
    return value


def excluded(path):
    return (any(p in {'.git', '__pycache__', 'node_modules', '.venv', '.hermes', '.cache',
                      '.locks', 'mcp-tokens', 'pairing', 'sessions'} or p.startswith('.web_dist-build-')
                for p in path.parts)
            or path.name.startswith('.env') and path.name not in {'.env.example', '.env.template'}
            or path.name.endswith(('.lock', '.pyc', '.pem', '.key'))
            or path.name in {'auth.json', 'nous_auth.json', 'google_token.json', 'google_client_secret.json'})


def snapshot(output):
    # Map archive names to bytes. Sorting and fixed metadata keep unchanged snapshots identical.
    entries = {}

    def add_file(path, name):
        if path.is_file() and not path.is_symlink():
            entries[str(name)] = (path.read_bytes(), 0o700 if os.access(path, os.X_OK) else 0o600)

    def add_tree(root, prefix):
        if root.exists():
            for path in sorted(root.rglob('*')):
                relative = path.relative_to(root)
                if not excluded(relative):
                    add_file(path, Path(prefix) / relative)

    for directory in ['skills', 'plugins', 'hooks', 'scripts', 'memories', 'kanban']:
        add_tree(HERMES / directory, 'hermes-home/' + directory)
    for name in ['SOUL.md', 'channel_directory.json', 'cron/jobs.json']:
        add_file(HERMES / name, 'hermes-home/' + name)
    config = yaml.safe_load((HERMES / 'config.yaml').read_text())
    entries['hermes-home/config.yaml'] = (yaml.safe_dump(clean_config(config), sort_keys=False).encode(), 0o600)
    env_keys = [line.split('=', 1)[0].strip() for line in (HERMES / '.env').read_text().splitlines()
                if '=' in line and not line.lstrip().startswith('#')]
    entries['hermes-home/.env.example'] = (''.join(k + '=\n' for k in env_keys).encode(), 0o600)
    # SQLite's backup API provides a consistent snapshot, including committed WAL data.
    with tempfile.TemporaryDirectory(dir=STATE) as database_dir:
        databases = list(HERMES.glob('*.db')) + list((HERMES / 'cron').glob('*.db'))
        for database in sorted(databases):
            dest = Path(database_dir) / database.name
            with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=10) as source_db:
                with sqlite3.connect(dest) as dest_db:
                    source_db.backup(dest_db)
            add_file(dest, 'hermes-home/' + str(database.relative_to(HERMES)))

    files = run('git', '-C', str(SOURCE), 'ls-files', '-z', '--cached', '--others', '--exclude-standard',
                stdout=subprocess.PIPE).stdout.split(b'\0')
    for raw in sorted(set(files)):
        if raw:
            relative = Path(os.fsdecode(raw))
            if not excluded(relative) or relative.name in {'uv.lock', 'flake.lock', 'yarn.lock'}:
                add_file(SOURCE / relative, Path('hermes-agent') / relative)
    metadata = {}
    for field, args in [('commit', ['rev-parse', 'HEAD']), ('branch', ['rev-parse', '--abbrev-ref', 'HEAD']),
                        ('upstream', ['remote', 'get-url', 'origin'])]:
        metadata[field] = run('git', '-C', str(SOURCE), *args, stdout=subprocess.PIPE).stdout.decode().strip()
    entries['source-version.json'] = (json.dumps(metadata, indent=2).encode() + b'\n', 0o600)
    add_tree(HOME / 'hermes-operator', 'hermes-operator')
    # Pairing QR images are credentials, too.
    entries = {k: v for k, v in entries.items() if not k.endswith(('.png', '.jpg')) or not k.startswith('hermes-operator/')}
    units = HOME / '.config/systemd/user'
    for path in sorted(units.glob('hermes*')):
        if path.is_file():
            text = '\n'.join(line for line in path.read_text().splitlines()
                             if not line.lstrip().startswith('Environment=')) + '\n'
            entries['systemd/' + path.name] = (text.encode(), 0o600)
        elif path.is_dir():
            # Override files can contain credentials: leave these for manual recreation.
            pass
    counts = {}
    for group in ['source', 'setup']:
        counts[group] = 0
        with tarfile.open(output / (group + '.tar'), 'w') as archive:
            for name, (data, mode) in sorted(entries.items()):
                is_source = name.startswith('hermes-agent/') or name == 'source-version.json'
                if is_source != (group == 'source'):
                    continue
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(data), mode, 0
                archive.addfile(info, io.BytesIO(data))
                counts[group] += 1
    return counts


def main():
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'backup.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another backup is already running', flush=True)
            return
        with tempfile.TemporaryDirectory(dir=STATE) as working:
            counts = snapshot(Path(working))
            summary_path = REPO / 'snapshot.json'
            summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
            for group, count in counts.items():
                archive = Path(working) / (group + '.tar')
                with archive.open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                previous = STATE / (group + '.sha256')
                chunks_exist = list(REPO.glob(group + '.tar.gpg.part-*'))
                if previous.exists() and previous.read_text().strip() == digest and chunks_exist:
                    print(group + ': unchanged', flush=True)
                    continue
                encrypted = Path(working) / (group + '.tar.gpg')
                run('gpg', '--batch', '--yes', '--pinentry-mode', 'loopback', '--passphrase-file', str(KEY),
                    '--symmetric', '--cipher-algo', 'AES256', '--compress-algo', 'ZLIB',
                    '--output', str(encrypted), str(archive))
                new_chunks = []
                with encrypted.open('rb') as stream:
                    while data := stream.read(45 * 1024 * 1024):
                        chunk = Path(working) / f'{group}.tar.gpg.part-{len(new_chunks):03d}'
                        chunk.write_bytes(data)
                        new_chunks.append(chunk)
                for old in chunks_exist:
                    old.unlink()
                for chunk in new_chunks:
                    chunk.replace(REPO / chunk.name)
                summary[group] = {'created_utc': datetime.now(timezone.utc).isoformat(), 'files': count,
                                  'encrypted_bytes': encrypted.stat().st_size, 'parts': len(new_chunks)}
                summary_path.write_text(json.dumps(summary, indent=2) + '\n')
                previous.write_text(digest + '\n')
                print(f'{group}: encrypted {count} files', flush=True)
        run('git', '-C', str(REPO), 'add', '--all')
        changes = subprocess.run(['git', '-C', str(REPO), 'diff', '--cached', '--quiet']).returncode
        if changes == 1:
            run('git', '-C', str(REPO), 'commit', '-m', 'Backup Hermes setup ' + datetime.now(timezone.utc).isoformat())
        elif changes:
            raise RuntimeError('Could not inspect staged changes')
        if '--local-only' not in os.sys.argv:
            if not (HOME / '.config/hermes-backup/github-token').exists():
                raise SystemExit('Local backup saved; GitHub upload pending: repository token not supplied')
            # Retry every run even if the snapshot was unchanged after a failed push.
            run('git', '-C', str(REPO), 'push', 'origin', 'HEAD:main', timeout=90)
            (STATE / 'last-success').write_text(datetime.now(timezone.utc).isoformat() + '\n')
            print('Backup pushed successfully', flush=True)


if __name__ == '__main__':
    main()
