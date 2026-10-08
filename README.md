# py_upper

`py_upper` 是一个面向 Windows、macOS、Linux 的独立 Python 应用运行时与打包工程。

当前稳定开发线：**0.17.5**。

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
│   ├── pyproject.toml.example  # 配置模板（入库）
│   ├── pyproject.toml          # 本地配置（Git ignored，从模板复制而来）
│   ├── src/
│   │   ├── main.py
│   │   └── ...
│   ├── tests/
│   └── resources/            # 可选：随包资源，没有该目录就不打包
├── launcher/
├── runtimes/                 # Git ignored，PBS/local runtime cache
├── tools/
│   ├── build.py              # 唯一公共构建入口
│   └── py_upper/             # 构建工具实现（模块清单见开发指南 §15）
├── .github/workflows/
├── build/                    # Git ignored
└── dist/                     # Git ignored
```

## 首次使用

应用配置不入库，克隆后先把模板复制成工作副本：

```bash
cp app/pyproject.toml.example app/pyproject.toml
```

`app/pyproject.toml` 被 `.gitignore` 忽略：应用名、依赖、runtime 版本这些跟着你机器走的东西写在里面，不会进 Git；`app/pyproject.toml.example` 是入库模板，跟着仓库走。忘了复制的话，构建工具会直接报错并把上面这条命令打出来。

## App 名称

项目名固定为 `py_upper`，最终 App 名称在你的本地配置 `app/pyproject.toml` 里独立配置：

```toml
[tool.py_upper.app]
name = "MyApp"
identifier = "com.example.pyupper"
```

`name` 是唯一来源：产物名（`.app` / 可执行文件 / 入口脚本 / `Info.plist` / Windows 版本资源）都由它派生，运行时 launcher 把同一个值编译进去并用 `PY_UPPER_APP_NAME` 导出。应用侧统一调 `utils.paths.app_name()` 读取——开发态没有 launcher 时它会回读这份 `pyproject.toml`——**不要在应用代码里再写一份名字**。

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

标准 Python 依赖直接写在你自己的 `app/pyproject.toml`。入库模板 `app/pyproject.toml.example` 的 `dependencies` 是空列表——具体依赖跟着应用走，克隆仓库的人不该继承别人的 pin：

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

## 启动器与错误可见性

`launcher/` 是不链接 libpython 的 C++ 可执行文件：运行时从 bundled runtime 解析 CPython 符号，所以换目标 Python 版本不需要重新链接。它按 `<可执行名>.int` → `.py` → `.pyw` → `_py_upper_static.int` 的顺序找入口；打包时会同时写出 `<App>.int` 和 `_py_upper_static.int`，因此**把可执行文件改名后应用照样能起来**。

launcher 还向应用导出 `PY_UPPER_HOME`（入口目录）、`PY_UPPER_RUNTIME`、`PY_UPPER_SITE_PACKAGES`、`PY_UPPER_SCRIPT`、`PY_UPPER_EXECUTABLE`，应用不必靠数父目录猜自己在哪。

致命错误先写 stderr，再按需弹原生对话框（Windows `MessageBoxW`、macOS `CFUserNotification`、Linux `zenity`/`xmessage`）——双击启动没有终端时，这是唯一能看到失败原因的通道。应用抛未捕获异常时，bootstrap 先把 traceback 落到 `TMPDIR/py_upper-<pid>.log`，`Py_FinalizeEx` 之后由 launcher 上报。CI 与自动化设 `PY_UPPER_NO_DIALOG=1`，失败只体现为退出码，不会卡在没人能点的对话框上。

Windows 默认是 GUI 子系统（双击不弹控制台），从 cmd/CI 启动时自动 `AttachConsole` 到父控制台，并且链接静态 CRT（`/MT`），目标机不需要 VC++ 运行库。完整契约与退出码见开发指南 §10。

## 验证

应用入口会打开 Qt 主窗口。CI 与 smoke 通过 `PY_UPPER_HEADLESS=1` 用 offscreen 平台构建同一个窗口后立即退出，因此既证明 Qt 可用，又不会卡住无显示环境。

`--verify` 是静态检查，`--run` 之前的最后一道门是**用目标 Python 真 import**：先由 bundled runtime 导入入口模块和所有直接声明的第三方依赖，再由打包好的 launcher 导入一次。这一步能抓住静态检查看不到的问题（扩展链接方式、stdlib 扩展目录、第三方依赖缺失、Qt 裁剪过度）。

CI 在每个原生平台上都跑同一套真实链路：单元测试 → 本地端到端构建（第三方 wheel、应用自带 native 库、lock/`--locked` 往返）→ doctor → 完整 PBS 构建 → 运行。本地 E2E 默认跳过，设置 `PY_UPPER_E2E=1` 打开。触发范围只有 develop（`push` 与面向 develop 的 `pull_request`）：`feature_*` 分支上的中间提交不跑矩阵，需要单独验证时用 `gh workflow run validate.yml --ref <branch>`。

## Git

Git 历史完整保留，包括之前的 `v0.16.x` 版本；v0.17.0 是一次结构性重构，不改写旧 tag。`runtimes/`、`build/`、`dist/` 和 Python cache 均由 `.gitignore` 忽略。
