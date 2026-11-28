"""检查中文维护说明和逐文件索引；只读检查，不自动生成注释。"""
from __future__ import annotations

import io
from pathlib import Path
import re
import subprocess
import tokenize


def main():
    """检查 Git 跟踪文件；缺少中文注释或索引条目时返回非零。"""
    root = Path(__file__).resolve().parents[1]
    files = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
    index = (root / 'docs/FILE_MAINTENANCE_INDEX.md').read_text(encoding='utf-8')
    errors = []
    count = 0
    for name in filter(None, files):
        count += 1
        path = root / name
        if f'](../{name})' not in index:
            errors.append(f'{name}: 缺少索引条目')
        text = path.read_text(encoding='utf-8')
        if path.suffix == '.py':
            # 仅检查注释和模块说明，避免把运行时中文字符串误算为注释。
            import ast
            doc = ast.get_docstring(ast.parse(text)) or ''
            comments = '\n'.join(t.string for t in tokenize.generate_tokens(io.StringIO(text).readline)
                                 if t.type == tokenize.COMMENT)
            if not re.search(r'[\u4e00-\u9fff]', doc + comments):
                errors.append(f'{name}: 缺少中文说明')
        elif path.suffix in {'.c', '.cpp', '.h'}:
            if not re.search(r'/\*\*.*?@brief[^\n]*[\u4e00-\u9fff].*?\*/', text, re.S):
                errors.append(f'{name}: 缺少中文 Doxygen 文件说明')
    print(f'检查 {count} 个跟踪文件，发现 {len(errors)} 个问题')
    for error in errors:
        print(error)
    return bool(errors)


if __name__ == '__main__':
    raise SystemExit(main())
