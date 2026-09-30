from __future__ import annotations
import os
import shutil
from pathlib import Path
from .config import APP, DIST, Target, project_version, staging_dir, target_runtime_dir
from .native.bundle import bundle_native_dependencies

ENTRY = '''from myapp.__main__ import main\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n'''


def package(target: Target, launcher: Path) -> Path:
    stage = staging_dir(target)
    runtime = target_runtime_dir(target)
    if not (stage / "site-packages").exists(): raise RuntimeError("Python stage missing")
    if not runtime.exists(): raise RuntimeError("Runtime missing")
    if DIST.exists(): shutil.rmtree(DIST)
    if target.os in {"windows", "linux"}:
        out = DIST / "MyApp"
        (out / "site-packages").mkdir(parents=True)
        (out / "runtime").mkdir(parents=True)
        (out / "resources").mkdir(parents=True)
        shutil.copy2(launcher, out / ("MyApp.exe" if target.os == "windows" else "MyApp"))
        shutil.copytree(runtime, out / "runtime", dirs_exist_ok=True)
        shutil.copytree(stage / "site-packages", out / "site-packages", dirs_exist_ok=True)
        shutil.copytree(APP / "resources", out / "resources", dirs_exist_ok=True)
        (out / "MyApp.int").write_text(ENTRY, encoding="utf-8")
        bundle_native_dependencies(out, target)
        return out
    if target.os == "macos":
        app = DIST / "MyApp.app"
        contents = app / "Contents"; macos = contents / "MacOS"; res = contents / "Resources"
        (res / "runtime").mkdir(parents=True); (res / "site-packages").mkdir(); (res / "resources").mkdir(); macos.mkdir(parents=True)
        shutil.copy2(launcher, macos / "MyApp"); os.chmod(macos / "MyApp", 0o755)
        shutil.copytree(runtime, res / "runtime", dirs_exist_ok=True)
        shutil.copytree(stage / "site-packages", res / "site-packages", dirs_exist_ok=True)
        shutil.copytree(APP / "resources", res / "resources", dirs_exist_ok=True)
        (res / "MyApp.int").write_text(ENTRY, encoding="utf-8")
        (contents / "Info.plist").write_text(f'''<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict><key>CFBundleExecutable</key><string>MyApp</string><key>CFBundleIdentifier</key><string>com.example.mypstand2</string><key>CFBundleName</key><string>MyApp</string><key>CFBundlePackageType</key><string>APPL</string><key>CFBundleVersion</key><string>{project_version()}</string><key>CFBundleShortVersionString</key><string>{project_version()}</string></dict></plist>\n''', encoding="utf-8")
        bundle_native_dependencies(app, target)
        return app
    raise RuntimeError(target.os)
