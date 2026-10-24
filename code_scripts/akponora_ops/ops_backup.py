"""Encrypted incremental Company A recovery sets; no accounting or network authority.

Uses SQLite's backup API, authenticated streaming encryption and isolated restore.
Only a configured mounted destination is used. Recovery keys are never copied into a set.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zlib
from datetime import datetime, timezone
from pathlib import Path

from code_scripts.akponora_ops.common import dump_json

MAGIC = b'NORABK01'
CHUNK = 1024 * 1024


def init_key(path: Path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(base64.urlsafe_b64encode(os.urandom(32)))
        f.flush(); os.fsync(f.fileno())


def read_key(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError('backup key must be a private regular file (mode 0600)')
    key = base64.b64decode(path.read_bytes().strip(), altchars=b'-_', validate=True)
    if len(key) != 32:
        raise ValueError('backup key must contain exactly 32 random bytes in base64')
    return key


def file_sha(path):
    sha = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(CHUNK), b''):
            sha.update(chunk)
    return sha.hexdigest()


def publish_encrypted(source: Path, destination: Path):
    """Container /tmp and the persistent volume can be different filesystems."""
    fd, name = tempfile.mkstemp(prefix='.', suffix='.tmp', dir=destination.parent)
    try:
        with Path(source).open('rb') as src, os.fdopen(fd, 'wb') as dst:
            shutil.copyfileobj(src, dst, CHUNK)
            dst.flush(); os.fsync(dst.fileno())
        os.replace(name, destination)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def encrypt_file(source: Path, destination: Path, key: bytes):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    iv = os.urandom(12)
    header = MAGIC + iv
    encryptor = Cipher(algorithms.AES(key), modes.GCM(iv)).encryptor()
    encryptor.authenticate_additional_data(header)
    compressor = zlib.compressobj(3, zlib.DEFLATED, 31)
    raw_sha = hashlib.sha256()
    with Path(source).open('rb') as src, Path(destination).open('xb') as dst:
        os.chmod(destination, 0o600)
        dst.write(header)
        for block in iter(lambda: src.read(CHUNK), b''):
            raw_sha.update(block)
            dst.write(encryptor.update(compressor.compress(block)))
        dst.write(encryptor.update(compressor.flush()))
        dst.write(encryptor.finalize())
        dst.write(encryptor.tag)
        dst.flush(); os.fsync(dst.fileno())
    return raw_sha.hexdigest()


def authenticated_plaintext(source: Path, destination: Path, key: bytes):
    """Authenticate the entire compressed message before decompression or restore use."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    with Path(source).open('rb') as src:
        size = src.seek(0, 2)
        if size < len(MAGIC) + 12 + 16:
            raise ValueError('truncated encrypted backup')
        src.seek(0); header = src.read(len(MAGIC) + 12)
        if not header.startswith(MAGIC):
            raise ValueError('unsupported backup format')
        src.seek(-16, 2); tag = src.read(16)
        src.seek(len(header))
        decryptor = Cipher(algorithms.AES(key), modes.GCM(header[len(MAGIC):], tag)).decryptor()
        decryptor.authenticate_additional_data(header)
        remaining = size - len(header) - 16
        with tempfile.TemporaryFile() as compressed:
            while remaining:
                block = src.read(min(CHUNK, remaining))
                if not block:
                    raise ValueError('truncated encrypted backup')
                remaining -= len(block)
                compressed.write(decryptor.update(block))
            compressed.write(decryptor.finalize())  # InvalidTag stops all restore use here.
            compressed.seek(0)
            inflater = zlib.decompressobj(31)
            with Path(destination).open('xb') as dst:
                os.chmod(destination, 0o600)
                for block in iter(lambda: compressed.read(CHUNK), b''):
                    dst.write(inflater.decompress(block))
                dst.write(inflater.flush())
                if not inflater.eof:
                    raise ValueError('incomplete compressed backup')


def read_manifest(path, key):
    with tempfile.TemporaryDirectory(prefix='nora-manifest-') as temp:
        decoded = Path(temp) / 'manifest.json'
        authenticated_plaintext(path, decoded, key)
        result = json.loads(decoded.read_text())
    if result.get('version') != 1 or result.get('complete') is not True:
        raise ValueError('backup manifest is incomplete or unsupported')
    return result


def sqlite_copy(source, destination):
    src = sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
        if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite recovery copy failed its integrity check')
    finally:
        src.close(); dst.close()


def company_a_files(root):
    root = Path(root).resolve()
    picks = [root / 'db.sqlite3', root / 'code_scripts/qbo_tokens.sqlite', root / 'company_a_proposals.sqlite',
             root / 'code_scripts/companies/company_a.json', root / 'code_scripts/uploaded_docnumbers.json']
    for folder in ('ops/company_a', 'mappings/company_a', 'approvals', 'conversion_preflight'):
        path = root / folder
        if path.exists():
            picks.extend(p for p in path.rglob('*') if p.is_file())
    # Company A archive metadata names its own source/transformed files in each day folder.
    for meta in (root / 'code_scripts/Uploaded').glob('*/last_epos_transform.json'):
        doc = json.loads(meta.read_text())
        if doc.get('company_key') != 'company_a':
            continue
        picks.append(meta)
        names = [doc.get('raw_file'), doc.get('raw_file_path'), *(doc.get('processed_files') or [])]
        picks.extend(meta.parent / Path(str(n)).name for n in names if n)
    for p in sorted(set(picks)):
        if not p.exists() or p.name.endswith(('-wal', '-shm', '.lock', '.tmp')):
            continue
        if p.is_symlink() or not p.resolve().is_relative_to(root):
            raise ValueError('backup source escaped the configured state root')
        yield p


def create(root: Path, destination: Path, key_file: Path, *, files=None):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    if files is None:
        required = ('db.sqlite3', 'code_scripts/qbo_tokens.sqlite', 'mappings/company_a/approved.csv',
                    'code_scripts/companies/company_a.json')
        if any(not (root / name).is_file() for name in required):
            raise ValueError('required Company A recovery source is missing')
    key = read_key(key_file)
    namespace = destination / hashlib.sha256(key).hexdigest()[:12]
    blobs = namespace / 'blobs'; manifests = namespace / 'manifests'
    for directory in (namespace, blobs, manifests):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    prior_paths = sorted(manifests.glob('*.nora.enc'))
    prior = read_manifest(prior_paths[-1], key)['files'] if prior_paths else {}
    entries, changed, reused = {}, 0, 0
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    with tempfile.TemporaryDirectory(prefix='nora-backup-') as temp:
        temp = Path(temp)
        for i, source in enumerate(files if files is not None else company_a_files(root)):
            source = Path(source)
            if source.is_symlink() or not source.resolve().is_relative_to(root):
                raise ValueError('backup source escaped the configured state root')
            source = source.resolve()
            if source.resolve() == Path(key_file).resolve() or source.resolve().is_relative_to(destination):
                continue
            relative = source.relative_to(root).as_posix()
            stat = source.stat(); old = prior.get(relative) or {}
            is_db = source.name.endswith(('.sqlite', '.sqlite3', '.db'))
            # A daily database copy is always transactionally consistent, never a raw live-file copy.
            if not is_db and old.get('mtime_ns') == stat.st_mtime_ns and old.get('size') == stat.st_size:
                blob = blobs / (old['sha256'] + '.enc')
                if blob.is_file() and file_sha(blob) == old['cipher_sha256']:
                    entries[relative] = old; reused += 1; continue
            working = source
            if is_db:
                working = temp / (str(i) + '.sqlite')
                sqlite_copy(source, working)
            enc = temp / (str(i) + '.enc')
            raw_sha = encrypt_file(working, enc, key)
            if not is_db and (source.stat().st_mtime_ns, source.stat().st_size) != (stat.st_mtime_ns, stat.st_size):
                raise ValueError('source changed during backup; retry under the global run lock')
            blob = blobs / (raw_sha + '.enc')
            if not blob.exists():
                publish_encrypted(enc, blob)
            elif not (old.get('sha256') == raw_sha and file_sha(blob) == old.get('cipher_sha256')):
                # Blobs are immutable: replacing one would invalidate earlier manifests.
                # Verify a pre-existing content identity before reusing it in another set.
                verified = temp / (str(i) + '.verified')
                authenticated_plaintext(blob, verified, key)
                if file_sha(verified) != raw_sha:
                    raise ValueError('existing backup blob has conflicting contents')
                verified.unlink()
            entries[relative] = {'sha256': raw_sha, 'cipher_sha256': file_sha(blob), 'size': working.stat().st_size,
                                 'mtime_ns': stat.st_mtime_ns, 'sqlite': is_db}
            changed += 1
        manifest = {'version': 1, 'company': 'company_a', 'complete': True, 'created_at': stamp, 'files': entries,
                    'shared_sqlite': 'portal/token databases are copied consistently as opaque shared infrastructure'}
        plain = temp / 'manifest.json'; plain.write_text(json.dumps(manifest, sort_keys=True))
        encrypted = temp / 'manifest.enc'; encrypt_file(plain, encrypted, key)
        final = manifests / (stamp + '.nora.enc'); publish_encrypted(encrypted, final)
    return {'manifest': str(final), 'files': len(entries), 'changed': changed, 'reused': reused,
            'encrypted': True, 'offsite_verified': False}


def restore(manifest_path: Path, key_file: Path, target: Path):
    manifest_path, target = Path(manifest_path), Path(target)
    if target.exists() and any(target.iterdir()):
        raise ValueError('restore target must be empty and isolated from production')
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = target.resolve()
    key = read_key(key_file); manifest = read_manifest(manifest_path, key)
    blobs = manifest_path.parent.parent / 'blobs'
    for name, entry in manifest['files'].items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or not (target / relative).resolve().is_relative_to(target):
            raise ValueError('unsafe restore path')
        blob = blobs / (entry['sha256'] + '.enc')
        if file_sha(blob) != entry['cipher_sha256']:
            raise ValueError('encrypted backup changed or is corrupt')
        output = target / relative; output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        authenticated_plaintext(blob, output, key)
        if file_sha(output) != entry['sha256']:
            raise ValueError('restored file checksum mismatch')
        if entry.get('sqlite'):
            with sqlite3.connect(output) as db:
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('restored SQLite database failed integrity validation')
    return {'files_restored': len(manifest['files']), 'integrity': 'verified', 'target': str(target)}


def copy_offsite(manifest_path: Path, destination: Path, key_file: Path):
    key = read_key(key_file); manifest_path = Path(manifest_path)
    doc = read_manifest(manifest_path, key)
    source = manifest_path.parent.parent
    target = Path(destination) / source.name
    (target / 'blobs').mkdir(parents=True, exist_ok=True, mode=0o700)
    (target / 'manifests').mkdir(parents=True, exist_ok=True, mode=0o700)
    for entry in doc['files'].values():
        name = entry['sha256'] + '.enc'; dst = target / 'blobs' / name
        if not dst.exists() or file_sha(dst) != entry['cipher_sha256']:
            tmp = dst.with_suffix('.tmp'); shutil.copyfile(source / 'blobs' / name, tmp); os.chmod(tmp, 0o600); os.replace(tmp, dst)
        if file_sha(dst) != entry['cipher_sha256']:
            raise ValueError('offsite encrypted copy failed checksum verification')
    dst = target / 'manifests' / manifest_path.name
    shutil.copyfile(manifest_path, dst); os.chmod(dst, 0o600)
    if file_sha(dst) != file_sha(manifest_path):
        raise ValueError('offsite manifest failed checksum verification')
    return {'manifest': str(dst), 'ciphertext_copy_verified': True, 'key_custody_verified': False}


def retain(manifest_path: Path, key_file: Path, *, daily=30, monthly=12):
    """Keep daily recovery points plus monthly points; authenticate ALL indexes before deletion."""
    if daily < 2 or monthly < 1:
        raise ValueError('retention requires at least two recent and one monthly recovery point')
    manifest_path = Path(manifest_path)
    key = read_key(key_file)
    paths = sorted(manifest_path.parent.glob('*.nora.enc'))
    docs = {p: read_manifest(p, key) for p in paths}
    recent_days, months, keep = set(), set(), set()
    for path in reversed(paths):
        stamp = docs[path]['created_at']
        day, month = stamp[:8], stamp[:6]
        if (day not in recent_days and len(recent_days) < daily) or (month not in months and len(months) < monthly):
            keep.add(path)
        recent_days.add(day); months.add(month)
    keep.update(paths[-2:])  # even repeated same-day tests retain two independently restorable points
    referenced = {entry['sha256'] + '.enc' for p in keep for entry in docs[p]['files'].values()}
    removed_sets = removed_blobs = 0
    for path in paths:
        if path not in keep:
            path.unlink(); removed_sets += 1
    for path in manifest_path.parent.parent.joinpath('blobs').glob('*.enc'):
        if path.name not in referenced:
            path.unlink(); removed_blobs += 1
    return {'sets_kept': len(keep), 'sets_removed': removed_sets, 'unreferenced_blobs_removed': removed_blobs}


def run_configured(root: Path):
    """Called after the financial routine, while it still owns the global lock."""
    config_path = Path(root) / 'ops/company_a/recovery/config.json'
    if not config_path.exists():
        return {'status': 'not configured', 'offsite_verified': False}
    config = json.loads(config_path.read_text())
    key_file = Path(config['key_file'])
    result = create(root, Path(config['destination']), key_file)
    manifest = Path(result['manifest'])
    # Prove recovery in an isolated temporary directory; never replace a running database.
    with tempfile.TemporaryDirectory(prefix='nora-isolated-restore-') as temp:
        proof = restore(manifest, key_file, Path(temp))
        result['restore_check'] = {'files_restored': proof['files_restored'], 'integrity': proof['integrity']}
    if config.get('offsite_destination'):
        result['offsite_copy'] = copy_offsite(manifest, Path(config['offsite_destination']), key_file)
    result['retention'] = retain(manifest, key_file, daily=int(config.get('daily', 30)), monthly=int(config.get('monthly', 12)))
    result['status'] = 'protected local copy; offsite acceptance pending' if not config.get('offsite_destination') else 'protected copies; separate key custody requires owner confirmation'
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('action', choices=('init-key', 'create', 'restore'))
    ap.add_argument('--key-file', type=Path, required=True)
    ap.add_argument('--state-root', type=Path, default=Path(os.environ.get('STATE_ROOT', 'runtime')))
    ap.add_argument('--destination', type=Path)
    ap.add_argument('--manifest', type=Path)
    a = ap.parse_args(argv)
    if a.action == 'init-key':
        init_key(a.key_file); result = {'key_created': True, 'key_value_returned': False}
    elif a.action == 'create':
        if not a.destination: ap.error('--destination is required')
        result = create(a.state_root, a.destination, a.key_file)
    else:
        if not a.manifest or not a.destination: ap.error('--manifest and --destination are required')
        result = restore(a.manifest, a.key_file, a.destination)
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
