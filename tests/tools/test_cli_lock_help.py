"""帮助查询无需获取构建锁；已有构建不能阻止用户查看参数。"""
import os
from pathlib import Path
import subprocess
import sys

from py_upper.workspace_lock import workspace_lock


def test_help_remains_available_while_build_is_locked():
    """持有真实工作区锁时，独立 CLI 进程仍返回帮助和零退出码。"""
    root = Path(__file__).resolve().parents[2]
    with workspace_lock(root):
        result = subprocess.run([sys.executable, 'tools/build.py', '--help'],
                                cwd=root, env=os.environ.copy(), capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '--app' in result.stdout
