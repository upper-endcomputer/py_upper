# py_upper

`py_upper` 是一个面向 Windows、macOS、Linux 的独立 Python 应用运行时与打包工程。

当前稳定开发线：**0.17.3**。

## 构建模型

Build Tool 不再把 Runtime、Cython、第三方 wheel、native 依赖和打包逻辑各自孤立处理，而是围绕统一的 Target Contract 组织完整流水线：

```text
Project config
    ↓
TargetSpec
    ├── target triple / architecture
    ├── target Python / ABI
    ├── wheel compatibility tags
    ├── extension suffix
    └── runtime + SDK contract
    ↓
Runtime + SDK
    ↓
Third-party wheels
    ↓
Application source → Cython → target native extensions
    ↓
Package staging
    ↓
Native dependency closure
    ↓
Static verification
    ↓
Target Python import smoke
    ↓
Launcher smoke
    ↓
Application run
```

核心原则：

- `app/src/` 直接就是应用源码，不再套额外项目包名。
- Build Tool Python 与最终 Target Python 完全独立；Build Tool 最低支持 Python 3.8。
- 应用自己的 Python 模块默认 Cython 化；`__init__.py` 保留为 package marker。
- 第三方依赖不 Cython 化，而是根据 Target 的 wheel compatibility tags 安装目标 wheel。
- `app/src/` 下的 `.dylib/.so/.dll/.pyd` 是正式应用输入，会参加 native dependency closure。
- `runtimes/` 是机器本地 Runtime 缓存，不进入 Git。
- 最终验证不仅检查文件存在，还会用 bundled Target Python 真正 import 应用和直接声明的第三方依赖。

## 文档

- 开发使用指南（环境、配置、打包、依赖、Qt、排错）：[docs/DEVELOPMENT_GUIDE.md](docs/DEVELOPMENT_GUIDE.md)
- 变更记录：[CHANGELOG.md](CHANGELOG.md) / [HISTORY.md](HISTORY.md)

## 项目结构

```text
py_upper/
├── app/
│   ├── pyproject.toml
│   ├── src/
│   │   ├── main.py
│   │   └── ...
│   ├── tests/
│   └── resources/
├── launcher/
├── runtimes/                 # Git ignored，PBS/local runtime cache
├── tools/
│   ├── build.py              # 唯一公共构建入口
│   └── py_upper/
├── .github/workflows/
├── build/                    # Git ignored
└── dist/                     # Git ignored
```

## App 名称

项目名固定为 `py_upper`，最终 App 名称独立配置：

```toml
[tool.py_upper.app]
name = "MyApp"
identifier = "com.example.pyupper"
```

## 构建

```bash
python tools/build.py
python tools/build.py --run
python tools/build.py --verify
python tools/build.py --target linux-x86_64
python tools/build.py --lock --target linux-x86_64
python tools/build.py --locked --target linux-x86_64
python tools/build.py --doctor
python tools/build.py --clean
python tools/build.py --release
```

`--run` 只允许运行当前宿主平台目标；交叉目标可以正常 build/verify，但不会冒充在当前机器运行。

## Target Python / PBS

PBS 模式只需要配置一个精确的目标 Python 版本：

```toml
[tool.py_upper.runtime]
provider = "pbs"
python = "3.11.10"
```

`[tool.py_upper.pbs].release` 可选。省略时，py_upper 使用轻量的精确版本 metadata 获取对应 PBS build tag，然后只请求该 release 来确认 runtime 与 full SDK；不会枚举巨大的 `/releases` 列表。

如需完全固定 release：

```toml
[tool.py_upper.pbs]
release = "20241016"
```

实际解析的 Runtime/SDK、wheel 集合和校验值可写入 `py_upper.lock.json`。`--locked` 只使用锁文件与本地缓存，不进行新的依赖解析。

PBS 网络请求对临时 HTTP 408/425/429/5xx 有有限次重试，并使用 `.part` 下载文件避免半截 archive 被当成完整缓存。

## 第三方依赖

标准 Python 依赖直接写在 `app/pyproject.toml`：

```toml
[project]
dependencies = [
    "PySide6==6.11.2",
    "requests==...",
]
```

py_upper 为目标 Python/平台下载兼容 wheel，并把完整 wheel 内容安装到最终 `site-packages`。第三方包不会进入 Cython 编译流程。

解析 wheel 时使用的是**目标解释器**（`runtimes/<target>/<python>` 里的那个），而不是构建机上的 Python。pip 的 `--platform`/`--python-version` 只影响 wheel 兼容标签，环境标记（`sys_platform`、`python_full_version` 等）永远按运行 pip 的解释器求值；用宿主 Python 解析会漏掉目标版本才需要的依赖（例如 `bleak` 在 Python 3.10 上需要 `async-timeout`）。交叉目标无法执行目标解释器，此时退回宿主解释器并显式传入标签参数。

下载使用 py_upper 自己的 pip 缓存（`.cache/pip`），重复构建和 CI 不会重新下载几百 MB 的 wheel。默认 socket 超时 120 秒、重试 10 次，可配置：

```toml
[tool.py_upper.dependencies]
find_links = ["wheelhouse"]
no_index = true
# PySide6 的 Linux wheel 是 manylinux_2_34 (x86_64) / manylinux_2_39 (aarch64)，
# 单个 baseline 覆盖不了两个架构，因此接受一个列表；这同时决定了发布包的
# 最低 glibc。
manylinux = ["manylinux_2_39", "manylinux_2_34"]
timeout = 120
retries = 10
```

也可用 `PY_UPPER_PIP_TIMEOUT` / `PY_UPPER_PIP_RETRIES` 覆盖。国内网络建议给本地构建设置镜像（CI 不受影响）：

```bash
export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
```

PySide6 这类带大量 Qt native libraries/plugins 的包会继续参加 native dependency scan；py_upper 不会只复制 Python 文件而丢掉 Qt `.dylib/.so/.pyd`。

## 第三方依赖优化

默认使用安全优化：删除 Python cache，并从**复制后的** bundled Runtime 中移除不属于最终应用运行链的 `pip/ensurepip`。不会修改 `runtimes/` 源缓存。

需要更激进的 site-packages 裁剪时：

```toml
[tool.py_upper.optimize]
profile = "aggressive"
remove_tests = true
remove_docs = true
strip_native = true
```

`strip_native` 默认关闭。开启后只在发布复制树中处理 native binary，并在 macOS 重写 install name 后重新做 ad-hoc signing；最终 `--release` 再使用正式 identity 签名。

不会根据一次静态 import 就擅自删除第三方模块，因为 Python/Qt 插件机制存在动态 import。

## 应用自带 native 库

可以直接放在 `app/src`：

```text
app/src/
├── main.py
└── config/
    ├── PCBUSB.dylib
    └── ...
```

它们会进入 staging，并参加架构检查、依赖闭包和 macOS `@rpath/@loader_path` 重定位。对于 dylib 自身的 `LC_ID_DYLIB`，scanner 不会错误地当成 `LC_LOAD_DYLIB` 依赖。

如果 Cython 扩展在链接阶段直接依赖自有 `.dylib`，仍需让 target compiler 在编译阶段找到该库；发布阶段负责的是复制、闭包和重定位。

## Qt 裁剪

PySide6 的 wheel 会带上整个 Qt，`QtWebEngineCore` 一个就约 450 MB。默认策略是按 **import 裁剪**：读取 `app/src` 里引用的 `PySide6.Qt*` 模块，用目标 runtime 解析出传递闭包，然后删掉没用到的 wrapper 模块、Qt 库、插件类别、QML 树、shiboken 的 QML 辅助库、Qt 工具二进制和工具翻译。

```toml
[tool.py_upper.optimize]
qt = "imports"          # 默认 "all"，即完整 Qt
# qt = ["QtWidgets", "QtCore", "QtGui"]   # 也可以显式指定，会按同样规则展开闭包
```

实测：PySide6 6.11.0 的 macOS arm64 应用从 **1.2 GB 降到 153 MB**（PySide6 本体 1.1 GB → 104 MB），窗口照常打开、`--verify` 通过、`codesign --verify --deep --strict` 通过；再开 `strip_native = true` 可到 141 MB。

限制：动态加载的模块（`importlib`、QML 文件里 import）静态看不到，这类应用请显式列模块；交叉目标无法执行目标解释器，会保留完整 Qt。

## 可选 native 组件排除

个别 wheel 会带上无法在 bundle 内满足依赖的可选组件，例如 PySide6 的 macOS wheel 把 ODBC/Mimer/PostgreSQL 三个 Qt SQL 驱动链接到构建机绝对路径，并引用了它自己没有附带的 `QtQuickShapesDesignHelpers` framework。这类组件可以在打包时显式丢弃：

```toml
[tool.py_upper.native]
exclude = [
    "PySide6/Qt/plugins/sqldrivers/*qsqlodbc*",
    "PySide6/Qt/qml/QtQuick/Shapes/DesignHelpers/*",
]
```

模式是相对打包后 `site-packages` 的 glob。没有排除配置时，未解析的原生依赖仍然是硬错误（不会静默产出坏包）。`*.dSYM` 调试符号包不属于运行负载，始终会被移除。

## Runtime 与开发 Python

```toml
[tool.py_upper.runtime]
provider = "pbs"
python = "3.13.15"
```

开发 Python 可以是 3.8–3.13 的另一个独立解释器。选择顺序：

1. `PY_UPPER_PYTHON` / 兼容旧变量 `PYSTAND_PYTHON`
2. `app/.venv`
3. 当前执行 `tools/build.py` 的 Python
4. `python` / `python3`

Windows XP 场景仍必须使用真正兼容 XP 的 custom/local CPython runtime；官方 CPython 3.8.10 本身不是 XP runtime。

## 验证

应用入口会打开 Qt 主窗口。CI 与 smoke 通过 `PY_UPPER_HEADLESS=1` 用 offscreen 平台构建同一个窗口后立即退出，因此既证明 Qt 可用，又不会卡住无显示环境。

`--verify` 是静态检查，`--run` 之前的最后一道门是**用目标 Python 真 import**：先由 bundled runtime 导入入口模块和所有直接声明的第三方依赖，再由打包好的 launcher 导入一次。这一步能抓住静态检查看不到的问题（扩展链接方式、stdlib 扩展目录、第三方依赖缺失、Qt 裁剪过度）。

CI 在每个原生平台上都跑同一套真实链路：单元测试 → 本地端到端构建（第三方 wheel、应用自带 native 库、lock/`--locked` 往返）→ doctor → 完整 PBS 构建 → 运行。本地 E2E 默认跳过，设置 `PY_UPPER_E2E=1` 打开。

## Git

Git 历史完整保留，包括之前的 `v0.16.x` 版本；v0.17.0 是一次结构性重构，不改写旧 tag。`runtimes/`、`build/`、`dist/` 和 Python cache 均由 `.gitignore` 忽略。
