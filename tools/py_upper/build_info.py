"""Build provenance, deliberately excluding configuration secrets."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import shutil
from .config import APP, APP_CONFIG, ROOT, python_version


def write_build_info(root, target, options):
    digest = hashlib.sha256()
    for path in sorted((APP / 'src').rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts:
            digest.update(path.relative_to(APP).as_posix().encode())
            digest.update(path.read_bytes())
    git = shutil.which('git')
    result = subprocess.run([git, 'rev-parse', 'HEAD'], cwd=ROOT,
                            capture_output=True, text=True, check=False) if git else None
    dirty = subprocess.run([git, 'diff', '--quiet'], cwd=ROOT, check=False).returncode != 0 if git else None
    data = {
        'format': 1, 'builder_commit': result.stdout.strip() if result and result.returncode == 0 else None,
        'builder_dirty': dirty,
        'build_python': sys.version.split()[0], 'target': target.key,
        'target_python': python_version(),
        'source_sha256': digest.hexdigest(),
        'config_sha256': hashlib.sha256(APP_CONFIG.read_bytes()).hexdigest(),
        'options': options,
        'reproducibility': 'input provenance; not a byte-identical build guarantee',
    }
    resources = root / 'Contents/Resources' if target.os == 'macos' else root
    resources.mkdir(parents=True, exist_ok=True)
    (resources / 'build-info.json').write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')
