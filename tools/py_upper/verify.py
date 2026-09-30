from __future__ import annotations
from .config import APP, DIST, Target, app_name, runtime_spec, staging_dir, target_runtime_dir
from .manifest import read_manifest, validate_manifest
from .native.inspect import verify_arch
from .python_build import module_name, selected_sources


def _find_extension(site, stem, target):
    suffixes = [".pyd"] if target.os == "windows" else [".so"]
    return [p for suffix in suffixes for p in site.rglob(stem + "*" + suffix)]


def verify(t: Target) -> int:
    runtime = target_runtime_dir(t)
    stage = staging_dir(t)
    checks: list[tuple[bool, str]] = [
        (runtime.exists(), "runtime"),
        ((stage / "site-packages").exists(), "application site-packages"),
    ]
    if runtime.exists():
        try:
            manifest = read_manifest(runtime)
            validate_manifest(manifest, t, runtime_spec(t))
            checks.append((True, f"runtime manifest format={manifest.get('format')}"))
        except Exception as exc:
            checks.append((False, f"runtime manifest: {exc}"))

    site = stage / "site-packages"
    for source in selected_sources():
        mod = module_name(source)
        candidates = _find_extension(site, source.stem, t)
        checks.append((bool(candidates), f"Cython extension {mod}"))
        if candidates:
            try:
                info = verify_arch(candidates[0], t)
                checks.append((True, f"native arch {mod}={info.arch}"))
            except Exception as exc:
                checks.append((False, f"native arch {mod}: {exc}"))
            spec = runtime_spec(t)
            if t.os == "windows":
                platform_tag = {"x86": "win32", "x86_64": "win_amd64", "arm64": "win_arm64"}[t.arch]
                expected = f".cp{spec.major_minor.replace('.', '')}-{platform_tag}.pyd"
            elif t.os == "linux":
                arch = {"x86_64": "x86_64", "arm64": "aarch64"}[t.arch]
                expected = f".cp{spec.major_minor.replace('.', '')}-{arch}-linux-gnu.so"
            else:
                expected = f".cp{spec.major_minor.replace('.', '')}-darwin.so"
            checks.append((candidates[0].name.endswith(expected), f"Python extension tag {mod}={expected}"))
        rel = source.relative_to(APP / "src")
        py = site / rel
        checks.append((not py.exists(), f"source removed {mod}"))

    name = app_name()
    if t.os in {"windows", "linux"}:
        out = DIST / name
        executable = out / (f"{name}.exe" if t.os == "windows" else name)
        checks += [(x.exists(), n) for x, n in [(executable, executable.name), (out / f"{name}.int", f"{name}.int"), (out / "runtime", "runtime")]]
    else:
        out = DIST / f"{name}.app"
        checks += [(x.exists(), n) for x, n in [(out / f"Contents/MacOS/{name}", "launcher"), (out / f"Contents/Resources/{name}.int", f"{name}.int")]]
        executable = out / f"Contents/MacOS/{name}"

    if executable.exists():
        try:
            info = verify_arch(executable, t)
            checks.append((True, f"launcher arch={info.arch}"))
        except Exception as exc:
            checks.append((False, f"launcher arch: {exc}"))

    bad = 0
    for ok, name_ in checks:
        print(("PASS" if ok else "FAIL"), name_)
        bad += not ok
    return int(bool(bad))
