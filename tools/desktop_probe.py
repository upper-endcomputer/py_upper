# 中文维护说明：在指定真实 Qt 平台测量窗口 exposed 并保存截图；不得把 offscreen 当成物理显示延迟。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""Run with a Python containing the app's Qt binding; never substitutes offscreen."""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--platform', required=True, choices=['xcb', 'wayland', 'windows', 'cocoa'])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.environ['QT_QPA_PLATFORM'] = args.platform
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/src'))
    started = time.perf_counter()
    from application import PyUpperApplication
    from utils.qt_compat import QtCore
    app = PyUpperApplication([])
    args.output.mkdir(parents=True, exist_ok=True)
    result = {'platform': app.qt_app.platformName(), 'kind': 'source window exposure; not physical display latency'}
    assert result['platform'] == args.platform
    timer = QtCore.QTimer()
    def check():
        window = app.mvvm_app.get_view('main')._window
        handle = window.windowHandle()
        if handle and handle.isExposed():
            result['window_exposed_ms'] = (time.perf_counter() - started) * 1000
            result['screenshot'] = window.grab().save(str(args.output / 'main.png'))
            timer.stop()
            app.quit()
        elif time.perf_counter() - started > 10:
            result['error'] = 'Window not exposed within 10 seconds'
            timer.stop()
            app.quit()
    timer.timeout.connect(check)
    timer.start(10)
    app.run()
    (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    return 0 if result.get('screenshot') and 'error' not in result else 1


if __name__ == '__main__':
    raise SystemExit(main())
