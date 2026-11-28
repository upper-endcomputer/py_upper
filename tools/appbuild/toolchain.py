# 中文维护说明：选择原生 MSVC、Unix 或 Zig 工具链；编译器架构与目标 Python ABI 必须一致。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import BUILD, Target
from .fs import make_executable, sha256
from .net import http_json


@dataclass(frozen=True)
class Toolchain:
    target: Target
    env: dict[str, str]
    compiler: str
    cxx: str
    linker: str
    deployment_target: str | None = None
    # Extra arguments that turn ``compiler`` into a working frontend command,
    # such as clang's cross-compilation target. None means the compiler string
    # is the complete command itself.
    compiler_args: tuple[str, ...] | None = None


# MSVC host/target pairs as vcvarsall.bat spells them. The host half matters:
# "amd64" is only correct when the build host itself is x64, and an ARM64
# runner has ARM64 host tools rather than x64 ones.
VCVARSALL_HOST_TARGET = {
    ("x86", "x86"): "x86",
    ("x86", "x86_64"): "x86_amd64",
    ("x86", "arm64"): "x86_arm64",
    ("x86_64", "x86"): "amd64_x86",
    ("x86_64", "x86_64"): "amd64",
    ("x86_64", "arm64"): "amd64_arm64",
    ("arm64", "x86"): "arm64_x86",
    ("arm64", "x86_64"): "arm64_amd64",
    ("arm64", "arm64"): "arm64",
}

# The Visual Studio component that installs the host toolset above.
VC_HOST_COMPONENT = {
    "x86": "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
    "x86_64": "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
    "arm64": "Microsoft.VisualStudio.Component.VC.Tools.ARM64",
}


def _host_arch() -> str:
    """规范化宿主 CPU 名称，供 MSVC 与 Zig 选择可执行工具。

    Normalize ``platform.machine()`` to the architecture names used here."""
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        return "x86_64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return "x86"


def _vswhere() -> Path | None:
    """查找标准 Visual Studio Installer 位置中的 vswhere，找不到返回 None。"""
    for value in (
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe",
    ):
        if value.exists():
            return value
    return None


def _vs_install(host_arch: str) -> Path | None:
    """查找包含宿主 C++ 工具集的 Visual Studio；显式 Zig 时跳过。

    The Visual Studio installation that owns the host C++ toolset."""
    vswhere = _vswhere()
    if not vswhere:
        return None
    if os.environ.get("APPBUILD_WINDOWS_TOOLCHAIN") == "zig":
        return None
    result = subprocess.run([str(vswhere), "-latest", "-products", "*", "-requires", VC_HOST_COMPONENT[host_arch], "-property", "installationPath"], capture_output=True, text=True, check=False)
    values = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if values:
        return Path(values[-1])
    # An installation that lacks the required component is reported as an empty
    # result, which the caller turns into the actionable "install Build Tools"
    # message. A non-zero exit means vswhere itself failed, and its output is
    # the only description of why, so surface that instead of hiding it.
    if result.returncode != 0:
        raise RuntimeError(f"vswhere failed (exit {result.returncode}):\n{_output_tail(result.stdout, result.stderr)}")
    return None


def _parse_windows_environment(text: str) -> dict[str, str]:
    """提取 set 输出中的变量赋值并大写键名，忽略 banner。

    ``NAME=VALUE`` pairs from ``set``, ignoring banners and progress output.

    Keys are upper-cased because Windows environment variables are
    case-insensitive and ``os.environ`` already stores them that way. ``set``
    echoes the name a variable happens to have in the environment block, and
    the one for the search path is ``Path``, not ``PATH``: keeping that casing
    would add a second entry next to ``os.environ``'s ``PATH`` when the two are
    merged, and every ``env.get("PATH")`` lookup would then miss the search
    path ``vcvarsall.bat`` built.
    """
    env: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key and key.strip() == key and " " not in key:
            env[key.upper()] = value
    return env


def _output_tail(*streams: str, limit: int = 40) -> str:
    """保留各输出流末尾的非空行，控制失败诊断长度。

    Last ``limit`` non-empty lines of the captured output, for diagnostics."""
    lines = [line for stream in streams for line in stream.splitlines() if line.strip()]
    return "\n".join(lines[-limit:]) or "(no output)"


def _msvc_environment(bat: Path, vc_target: str) -> dict[str, str]:
    """通过 stdin 执行 vcvarsall 并核验关键变量；不能仅依赖批处理退出码。

    The environment ``vcvarsall.bat`` produces, validated instead of trusted.

    The script is fed to ``cmd.exe`` on stdin rather than passed on the command
    line. ``subprocess`` quotes arguments the way the C runtime expects, which
    is not how ``cmd.exe`` parses them: the quoted batch path arrives as
    ``\"C:\\Program Files\\...\\vcvarsall.bat\"``, cmd.exe fails to find that
    literal name, and the ``&& set`` that would have printed the environment
    never runs. Reading the script from stdin removes command-line quoting from
    the picture entirely.

    The batch file's exit status is not a usable success signal: it reports
    whatever internal command ran last, so a working initialization can exit
    non-zero. What matters is the postcondition, so require the variables
    vcvarsall must define and surface its own output when they are missing.
    Nothing is redirected to NUL, because vcvarsall reports its errors on
    stdout, where they are the only way to tell a missing SDK from a missing
    toolset.
    """
    result = subprocess.run(
        ["cmd.exe", "/d", "/q"],
        input=f'@call "{bat}" {vc_target}\r\n@set\r\n',
        capture_output=True,
        text=True,
        check=False,
    )
    env = _parse_windows_environment(result.stdout)
    if not env.get("VCTOOLSINSTALLDIR") or not env.get("INCLUDE"):
        raise RuntimeError(
            f"vcvarsall.bat did not initialize the MSVC environment "
            f"({bat} {vc_target}, exit {result.returncode}):\n"
            f"{_output_tail(result.stdout, result.stderr)}"
        )
    return env


# A pinned, executable Zig release replaces MSVC when Visual Studio is absent.
# The compiler always runs on the build host, so the archive is selected by the
# host platform and the Windows target is selected with clang's -target flag.
# Zig bundles the Windows headers and static CRT the launcher links. The
# archive identity is verified against the upstream index before unpacking.
ZIG_VERSION = os.environ.get("APPBUILD_ZIG_VERSION", "0.16.0")
ZIG_INDEX_URL = "https://ziglang.org/download/index.json"
ZIG_HOST_KEYS = {
    "x86_64": {"Windows": "x86_64-windows", "Darwin": "x86_64-macos", "Linux": "x86_64-linux"},
    "arm64": {"Windows": "aarch64-windows", "Darwin": "aarch64-macos", "Linux": "aarch64-linux"},
    "x86": {"Windows": "x86-windows", "Linux": "x86-linux"},
}


def _zig_host_key(host_os: str, host_arch: str) -> str:
    """将宿主系统与架构转换为 Zig 下载索引键；不支持的组合报错。"""
    key = ZIG_HOST_KEYS.get(host_arch, {}).get(host_os)
    if not key:
        raise RuntimeError(f"No executable Zig release for host {host_os}/{host_arch}")
    return key


def _zig_entry(entry: dict) -> tuple[str, str]:
    """提取下载地址和摘要；索引字段缺失时报错。"""
    tarball = str(entry.get("tarball") or "").strip()
    shasum = str(entry.get("shasum") or "").strip()
    if not tarball or not shasum:
        raise RuntimeError("Zig download index entry is missing a tarball or shasum")
    return tarball, shasum


def _zig_release(host_os: str, host_arch: str) -> tuple[str, str]:
    """从官方索引解析固定版本对应的宿主资产，不静默切换版本。"""
    try:
        index = http_json(ZIG_INDEX_URL)
    except Exception as exc:
        raise RuntimeError(f"Unable to resolve Zig {ZIG_VERSION} from {ZIG_INDEX_URL}: {exc}") from exc
    entry = index.get(ZIG_VERSION) if isinstance(index, dict) else None
    if not isinstance(entry, dict):
        raise RuntimeError(f"Zig {ZIG_VERSION} is not published in {ZIG_INDEX_URL}; update ZIG_VERSION in tools/appbuild/toolchain.py")
    return _zig_entry(entry.get(_zig_host_key(host_os, host_arch)) or {})


def _zig_exe_name() -> str:
    # The executable is named by the host the compiler runs on: the Windows
    # release ships zig.exe, every other release ships zig.
    """按运行编译器的宿主系统选择 Zig 可执行文件名。"""
    return "zig.exe" if platform.system() == "Windows" else "zig"


def _safe_archive_members(archive: Path):
    """逐项校验 ZIP/tar 成员名称并产出文件流；调用者须在推进迭代前消费并关闭流。"""
    if archive.suffix == ".zip":
        import zipfile
        with zipfile.ZipFile(archive) as zf:
            members = zf.infolist()
            names = {entry.filename for entry in members}
            if len(names) != len(members):
                raise RuntimeError(f"Duplicate member in Zig archive: {archive.name}")
            for entry in members:
                name = entry.filename
                if entry.is_dir():
                    continue
                if (not name or name.endswith("/") or name in {".", ".."}
                        or chr(92) in name or name.startswith("/")
                        or ".." in Path(name).parts):
                    raise RuntimeError(f"Unsafe Zig archive path: {name}")
                with zf.open(entry) as stream:
                    yield name, stream
        return
    import tarfile
    with tarfile.open(archive, "r:*") as tf:
        seen = set()
        for entry in tf:
            name = entry.name
            if name in seen:
                raise RuntimeError(f"Duplicate member in Zig archive: {archive.name}")
            seen.add(name)
            if not entry.isfile():
                continue
            if (not name or name in {".", ".."} or chr(92) in name
                    or name.startswith("/") or ".." in Path(name).parts):
                raise RuntimeError(f"Unsafe Zig archive path: {name}")
            stream = tf.extractfile(entry)
            if stream is None:
                continue
            yield name, stream


def _find_zig(root: Path) -> Path:
    """在已解压树中寻找宿主 Zig 可执行文件；不存在时报错。"""
    name = _zig_exe_name()
    candidates = [path for path in root.rglob(name) if path.is_file()]
    if not candidates:
        raise RuntimeError(f"{name} not found after unpacking the Zig archive under {root}")
    return candidates[0]


# Zig's own architecture names, for the components where the packaging triples
# disagree with them.
_ZIG_TRIPLE_ARCH = {"i686": "x86"}


def windows_gnu_cpython_defines(target: Target) -> tuple[str, ...]:
    r"""补齐 GNU 前端使用 MSVC 形状 CPython 头文件时的指针宽度和 snprintf 宏。

    The defines a GNU driver needs for the Windows target's CPython headers.

    The SDK ships the MSVC-shaped ``pyconfig.h`` of the official Windows build,
    and two of its statements are wrong for a MinGW-w64 ABI:

    * The pointer width is spelled out only on the branch pyconfig.h reaches
      through ``_MSC_VER``
      (``#ifdef _MSC_VER`` / ``#ifdef _WIN64`` / ``#define MS_WIN64``), and
      ``SIZEOF_VOID_P`` is only then 8.  A GNU driver never defines ``_MSC_VER``,
      so a 64-bit target fell through to the ``MS_WIN32`` branch with
      ``SIZEOF_VOID_P 4`` (and ``SIZEOF_SIZE_T 4``) -- a lie about the ABI of a
      runtime whose ``PyConfig`` the launcher shares.  Cython's own guard,
      ``1 / (int)(SIZEOF_VOID_P == sizeof(void*))``, was what failed loudly.
    * ``pyerrors.h`` renames ``snprintf`` to ``_snprintf`` under
      ``defined(MS_WIN32) && !defined(HAVE_SNPRINTF)``, which is right for
      MSVC's pre-C99 CRT and wrong for MinGW-w64's C99 one: ``std::snprintf``
      became ``std::_snprintf`` and the launcher did not compile.

    Both corrections are properties of the target ABI, so they come from the
    toolchain and every consumer -- the Cython extensions and the launcher --
    compiles against the same view of the headers.
    """
    defines = ["-DHAVE_SNPRINTF"]
    if target.arch in {"x86_64", "arm64"}:
        defines.append("-DMS_WIN64")
    return tuple(defines)


def ensure_zig(target: Target) -> Toolchain:
    """校验并缓存宿主原生 Zig，生成带目标 triple 的 Windows 编译前端。

    Download and unpack the pinned host-native Zig, then cross-compile.

    The compiler must run on the build host while emitting target code, so the
    archive is selected by host platform and the Windows target is passed with
    clang's -target flag. Everything lives under
    build/host-tools/zig/<version>-<host>; a verified archive marker means a
    previous build already unpacked this exact release.
    """
    host_os = platform.system()
    host_arch = _host_arch()
    if host_os not in {"Windows", "Darwin", "Linux"}:
        raise RuntimeError(f"Windows targets with Zig require a Windows/macOS/Linux build host, not {host_os}")
    host_key = _zig_host_key(host_os, host_arch)
    root = BUILD / "host-tools" / "zig" / f"{ZIG_VERSION}-{host_key}"
    if not (root / ".verified").is_file():
        url, expected = _zig_release(host_os, host_arch)
        archive = root / url.rsplit("/", 1)[-1]
        from .net import download

        download(url, archive)
        actual = sha256(archive)
        if actual.lower() != expected.lower():
            raise RuntimeError(f"Zig SHA256 mismatch for {url}: expected {expected}, got {actual}")
        marker = root / ".verified"
        if marker.exists():
            marker.unlink()
        extract = root / "archive"
        if extract.exists():
            shutil.rmtree(extract)
        extract.mkdir(parents=True)
        for name, stream in _safe_archive_members(archive):
            destination = extract / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with stream, destination.open("wb") as out:
                shutil.copyfileobj(stream, out)
        zig = _find_zig(extract)
        if host_os != "Windows":
            make_executable(zig)
        marker.write_text(f"{ZIG_VERSION}\n{host_key}\n{expected}\n", encoding="utf-8")
    else:
        zig = _find_zig(root)
    if host_os != "Windows":
        make_executable(zig)
    env = dict(os.environ)
    env["PATH"] = str(zig.parent) + os.pathsep + env.get("PATH", "")
    # Zig 0.16 does not bundle the MSVC ABI's CRT for cross builds; the GNU
    # ABI's bundled MinGW-w64 CRT links the same python313.lib import library
    # and produces a loadable extension, so that is the ABI used here.
    # Zig 0.16 rejects the "pc" vendor for Windows targets, so the compiler
    # target is spelled arch-windows-gnu (or arch-windows-msvc when Zig gains
    # MSVC CRT support for cross builds).
    triple = target.triple
    if triple.endswith("-pc-windows-msvc"):
        triple = triple[:-len("-pc-windows-msvc")] + "-windows-gnu"
    # Zig names the 32-bit x86 architecture "x86" and rejects the packaging
    # triple's leading "i686" as an unknown architecture.  Every other
    # component already matches Zig's spelling.
    arch, separator, remainder = triple.partition("-")
    zig_triple = _ZIG_TRIPLE_ARCH.get(arch, arch) + separator + remainder
    # The CPython headers of this SDK describe an MSVC build; the corrections
    # that make them describe the ABI the GNU driver actually emits are shared
    # with every consumer of the toolchain, so they ride in the frontend
    # spelled out below.
    zig_cc = ("cc", "-target", zig_triple, *windows_gnu_cpython_defines(target))
    zig_cxx = ("c++", "-target", zig_triple, *windows_gnu_cpython_defines(target))
    env.update({
        "CC": " ".join((str(zig), *zig_cc)),
        "CXX": " ".join((str(zig), *zig_cxx)),
    })
    return Toolchain(target, env, str(zig), env["CXX"], env["CC"], compiler_args=zig_cc)


def resolve_toolchain(target: Target) -> Toolchain:
    """根据宿主与目标选择 MSVC、Zig 或 Unix 工具链，拒绝未支持的交叉组合。"""
    host_os = platform.system()
    host_arch = _host_arch()
    if target.os == "windows":
        if host_os != "Windows" and os.environ.get("APPBUILD_WINDOWS_TOOLCHAIN") != "zig":
            raise RuntimeError("Windows targets require a Windows/MSVC build host; set APPBUILD_WINDOWS_TOOLCHAIN=zig to opt into executable Zig")
        install = _vs_install(host_arch)
        if not install:
            return ensure_zig(target)
        bat = install / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat"
        if not bat.exists():
            raise RuntimeError(f"vcvarsall.bat not found: {bat}")
        vc_target = VCVARSALL_HOST_TARGET.get((host_arch, target.arch))
        if not vc_target:
            raise RuntimeError(f"Unsupported MSVC host/target pair: {host_arch} -> {target.arch}")
        env = _msvc_environment(bat, vc_target)
        return Toolchain(target, env, env.get("CC", "cl"), env.get("CXX", "cl"), "link.exe")

    if target.os == "macos":
        if host_os != "Darwin":
            raise RuntimeError("macOS targets require a macOS build host")
        clang = shutil.which("clang")
        clangxx = shutil.which("clang++")
        if not clang or not clangxx:
            raise RuntimeError("Xcode Command Line Tools with clang/clang++ are required")
        arch = target.arch
        env = dict(os.environ)
        env.update({"CC": clang, "CXX": clangxx})
        env["MACOSX_DEPLOYMENT_TARGET"] = os.environ.get("MACOSX_DEPLOYMENT_TARGET", "11.0" if arch == "arm64" else "10.15")

        return Toolchain(target, env, clang, clangxx, clang, env["MACOSX_DEPLOYMENT_TARGET"])

    if target.os == "linux":
        if host_os != "Linux":
            raise RuntimeError("Linux targets require a Linux build host")
        native_arm = host_arch == "arm64"
        native_x64 = host_arch == "x86_64"
        if target.arch == "x86_64" and native_x64:
            cc, cxx = shutil.which("gcc"), shutil.which("g++")
        elif target.arch == "arm64" and native_arm:
            cc, cxx = shutil.which("gcc"), shutil.which("g++")
        elif target.arch == "x86_64":
            cc, cxx = shutil.which("x86_64-linux-gnu-gcc"), shutil.which("x86_64-linux-gnu-g++")
        else:
            cc, cxx = shutil.which("aarch64-linux-gnu-gcc"), shutil.which("aarch64-linux-gnu-g++")
        if not cc or not cxx:
            raise RuntimeError(f"Linux {target.arch} compiler is unavailable for host {host_arch}")
        env = dict(os.environ)
        env.update({"CC": cc, "CXX": cxx})
        return Toolchain(target, env, cc, cxx, cc)
    raise RuntimeError(f"Unsupported target: {target.key}")


def describe_toolchain(target: Target) -> dict[str, str]:
    """格式化已解析工具链的诊断信息。"""
    tc = resolve_toolchain(target)
    return {
        "target": target.key,
        "compiler": tc.compiler,
        "cxx": tc.cxx,
        "linker": tc.linker,
        "deployment_target": tc.deployment_target or "",
    }


def windows_import_library(target_python) -> tuple[Path, Path, str]:
    """查找目标版本的 CPython import library；跳过仅有稳定 ABI 库的目录，返回 include、libdir 和库名。

    The target Python include directory, import library directory and name.

    A CPython tree carries two import libraries: the versioned one
    (python313.lib, the ABI of the interpreter being targeted) and the
    stable-ABI shim (python3.lib, a subset of the same exports). Link against
    the versioned one, so the extension imports the DLL the runtime actually
    ships and the closure resolves it as a system dependency. Sort order used
    to decide this, and it picked the shim.

    The library is a compiler input, so it is read from the SDK tree. The
    packaged runtime is not a link input and does not carry one: the Windows
    release runtime is the python.org embeddable layout, which ships
    python3XX.dll and no libs/ directory whatsoever.
    """
    candidates = [
        target_python.sdk_root / "libs",
        target_python.sdk_root / "install" / "libs",
        target_python.sdk_root / "lib",
        target_python.sdk_root / "install" / "lib",
    ]
    python_lib = f"python{target_python.python_major_minor.replace('.', '')}"
    libdir = next((path for path in candidates if (path / f"{python_lib}.lib").is_file()), None)
    if libdir is None:
        raise RuntimeError(
            f"{python_lib}.lib not found under the "
            f"SDK tree {target_python.sdk_root}; it is a compiler input and is never taken from the runtime"
        )
    return target_python.include_dir, libdir, python_lib
