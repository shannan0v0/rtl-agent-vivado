"""Verify a recorded preinstalled package without inventing source/build provenance."""
from pathlib import Path, PurePosixPath
import hashlib

def validate_preinstalled_package(cpp, record, *, verify_hashes=True):
    if record.get('status') != 'HISTORICAL_NATIVE_PACKAGE_FINGERPRINTS_NOT_RUNTIME_ACCEPTANCE':
        raise ValueError('unrecognized package inventory')
    if cpp.get('source_commit') is not None or cpp.get('hip_rocm_version') is not None:
        raise ValueError('preinstalled package must retain unknown source/build fields')
    root = Path(record.get('package_root', ''))
    if root != Path('/opt/llamacpp') or root != root.resolve():
        raise ValueError('preinstalled package root mismatch')
    version = record.get('version_query', {})
    if version.get('rc') != 0 or not version.get('text', '').startswith('version: ' + cpp['version'] + '\n'):
        raise ValueError('captured package version mismatch')
    rows = record.get('files')
    if not isinstance(rows, list) or not rows:
        raise ValueError('empty package inventory')
    names = set()
    for row in rows:
        name = row.get('path'); rel = PurePosixPath(name) if isinstance(name, str) else None
        if not rel or rel.is_absolute() or '..' in rel.parts or '\\' in name or name in names:
            raise ValueError('unsafe or duplicate package path')
        names.add(name); p = root / name
        digest = row.get('sha256'); size = row.get('bytes')
        if not isinstance(digest, str) or len(digest) != 64 or any(x not in '0123456789abcdef' for x in digest):
            raise ValueError('missing package content digest')
        if isinstance(size, bool) or not isinstance(size, int) or size < 0 or row.get('symlink') is not False:
            raise ValueError('invalid recorded package file')
        if p.is_symlink() or p.resolve() != p or not p.is_file() or str(p) != row.get('resolved') or p.stat().st_size != size:
            raise ValueError('package file binding mismatch: ' + name)
        if verify_hashes:
            h = hashlib.sha256()
            with p.open('rb') as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b''): h.update(chunk)
            if h.hexdigest() != digest: raise ValueError('package digest mismatch: ' + name)
    if not {'llama-server', 'libllama-server-impl.so', 'libllama.so', 'libggml-hip.so'} <= names:
        raise ValueError('engine implementation files missing')
    launcher = next(row for row in rows if row['path'] == 'llama-server')
    if str(root / 'llama-server') != cpp['binary_path'] or launcher['sha256'] != cpp['binary_sha256']:
        raise ValueError('package launcher and asset identity differ')
