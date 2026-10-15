# py_upper 开发使用指南

本文是 `py_upper` 的完整开发与使用手册：从环境准备、日常开发、打包发布，到 Qt 应用处理、依赖管理和排错。README 讲的是"是什么"，本文讲的是"怎么用、为什么这么设计、出问题怎么查"。

当前稳定线：**0.17.9**。

---

## 1. 心智模型

先建立三个概念，后面所有内容都围绕它们：

```
宿主 Python（Build Tool 运行的解释器，3.8+）
        │  只负责：解析配置、跑 Cython、调 pip、调编译器
        ▼
Target Contract（目标契约：OS / 架构 / Python 版本 / ABI / wheel 标签 / 扩展后缀）
        │  所有平台差异的唯一来源
        ▼
目标产物（.app / 目录）：bundled CPython + site-packages + launcher
```

关键点：

- **宿主 Python 和目标 Python 完全解耦。** 你可以用 3.11 的开发机给 3.10 的目标打包，甚至交叉到别的架构。目标是哪个版本，只在 `app/pyproject.toml` 里写一次。
- **平台差异只在 Target Contract 里表达。** 代码里所有 `if target.os == ...` 都从 `Target`/`TargetPython` 取值，不再各自判断。
- **自己的代码 Cython 化，第三方库原样安装。** `app/src/**/*.py` 编译成 `.so/.pyd`（`__init__.py` 保留为包标记），第三方依赖按目标平台的 wheel 解包进 `site-packages`。

构建流水线（`python tools/build.py`）：

```
[1/6] Runtime + SDK        下载/复用目标 CPython（PBS 或本地目录）
[2/6] Third-party wheels   解析并安装目标平台 wheel + 增量编译应用模块
[3/6] Target launcher      CMake + Ninja 编译 C++ 启动器
[4/6] Package              复制运行时、site-packages、可选资源 + 原生依赖闭包 + 签名
[5/6] Static verification  文件、架构、ABI 后缀、依赖清单
[6/6] Runtime smoke        用 bundled Python 真 import + 启动器 smoke + 可选运行
```

---

## 2. 环境准备

### 2.1 宿主平台与工具

| 目标 | 宿主要求 | 额外工具 |
|---|---|---|
| macOS（arm64 / x86_64） | macOS | Xcode Command Line Tools（`clang`）、`cmake`、`ninja` |
| Windows（x86 / x86_64 / arm64） | Windows | Visual Studio Build Tools（含 C++）、`cmake`；无 VS 的 macOS/Linux 宿主可用 Zig 交叉构建 |
| Linux（x86_64 / arm64） | Linux | `gcc`/`g++`、`cmake`、`ninja`、`binutils`（可选 `patchelf`） |

交叉目标（例如在 macOS 上打 Linux 包）可以 build/verify，但不会冒充在当前机器运行；带平台标记的第三方依赖需要目标平台的宿主，见 §8.1。

**Windows 的 Zig 回退（无 VS 构建）：** 在 macOS/Linux 宿主上设置 `PY_UPPER_WINDOWS_TOOLCHAIN=zig`，构建工具会自动下载、SHA256 校验并使用**宿主原生** Zig（`PY_UPPER_ZIG_VERSION`，默认 `0.16.0`），用 `zig cc -target <arch>-windows-gnu` 交叉编译 launcher 和 Cython 扩展。注意：
- 产物使用 MinGW-w64 CRT/ABI（Zig 0.16 不提供 MSVC 交叉 CRT），与 CPython 官方 `python313.lib` 兼容；
- launcher 的子系统由 `launcher/CMakeLists.txt` 显式要求（非 MSVC 链接器传 `-Wl,--subsystem,windows`）——CMake 的 `WIN32_EXECUTABLE` 只对 MSVC 生效，clang 驱动会静默丢掉 `-mwindows`；`verify` 会断言产物子系统，默认要 GUI；
- `PY_UPPER_LAUNCHER_CONSOLE=1` 构建控制台子系统的调试版 launcher（stdout/stderr 常连），此时 `verify` 期望的子系统随之变成 console；
- 交叉目标不执行目标运行时/launcher 冒烟，静态验证照常执行；
- 有 VS 的 Windows 宿主不受影响（MSVC 路径不变）。

### 2.2 开发 Python（Build Tool 的解释器）

最低 **3.8**，推荐 3.10+。选择顺序：

1. `PY_UPPER_PYTHON`（旧变量 `PYSTAND_PYTHON` 仍兼容）
2. `app/.venv`
3. 当前执行 `tools/build.py` 的解释器
4. `python` / `python3`

检查当前生效的解释器：

```bash
python tools/build.py --doctor
```

Build Tool 不需要预装 Cython：缺失时会自动装到 `build/host-tools/cython-py<X>_<Y>/`，不会污染你的开发环境。

### 2.3 网络

PBS SDK、Windows python.org embeddable ZIP 与 PyPI wheel 都走 HTTPS。国内网络建议给本地构建设镜像（不影响 CI）：

```bash
export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
```

pip 的下载缓存放在 `.cache/pip`，重复构建不会重下几百 MB 的 wheel。

---

## 3. 快速开始

```bash
# 0. 克隆后先建本地配置（app/pyproject.toml 不入库）
cp app/pyproject.toml.example app/pyproject.toml

# 1. 写代码：app/src 就是应用源码
app/src/
├── main.py            # 入口（由 [tool.py_upper].entry 指定），只把控制权交给 application
├── application.py     # ★ 装配处：MVVM 的对象图在这里接起来（见 §9.4）
├── mvvm/              # MVVM 骨架：core / models / view_models / views / services
├── utils/             # 运行时胶水：paths（launcher 契约）、qt_compat（Qt 名字唯一入口）
└── ...

# 2. 声明依赖与目标
#    app/pyproject.toml（本地副本，改了不进 Git）
#    [project] dependencies = ["PySide6==6.11.0"]
#    [tool.py_upper.runtime] provider = "pbs"  python = "3.13.5"

# 3. 构建 + 运行
python tools/build.py --run

# 4. 只构建
python tools/build.py
```

产物：

```
dist/MyApp.app/Contents/          # macOS
├── MacOS/MyApp                   # launcher（可执行）
└── Resources/
    ├── MyApp.int                 # 入口脚本
    ├── runtime/                  # bundled CPython
    ├── site-packages/            # 你的模块 + 第三方
    └── resources/                # app/resources 的内容（没有该目录时不生成）

dist/MyApp/                       # Windows / Linux
├── MyApp(.exe)                   # launcher
├── MyApp.int
├── runtime/                      # Windows PBS：官方 python.org embed ZIP（pythonXY.zip、.pyd、DLL）
├── site-packages/
└── resources/                    # 同上，可选
```

---

## 4. 目录结构

```text
py_upper/
├── app/
│   ├── pyproject.toml.example # 配置模板（入库）
│   ├── pyproject.toml         # 本地配置（Git ignored，从模板复制）
│   ├── src/                   # ★ 应用源码（直接是包根）
│   ├── tests/                 # 应用测试 + 构建链测试
│   └── resources/             # 可选：随包资源，没有该目录就不打包
├── launcher/                 # C++ 启动器（CMake）
├── tools/
│   ├── build.py              # ★ 唯一公共构建入口
│   └── py_upper/             # 构建工具实现（见 §15）
├── runtimes/                 # 本地 runtime 缓存（git 忽略）
├── .cache/                   # PBS / pip 缓存（git 忽略）
├── build/                    # 中间产物（git 忽略）
└── dist/                     # 最终产物（git 忽略）
```

---

## 5. 配置参考（`app/pyproject.toml`）

仓库里入库的是模板 `app/pyproject.toml.example`，实际生效的是 Git 忽略的 `app/pyproject.toml`（`cp app/pyproject.toml.example app/pyproject.toml`）。下面的内容对两者都成立：模板改了就进 Git，本地副本改了只影响你这台机器。文件缺失时 Build Tool 会报错并给出复制命令，不会静默使用模板。

模板里的 `[project] dependencies` 是空列表：模板入库、依赖入库就变成"克隆的人继承维护者的 pin"。要装的包写进本地副本即可；CI 构建仓库自带示例应用时自己注入依赖集合（见 §11）。

```toml
[project]
name = "py_upper"
version = "0.17.9"
requires-python = ">=3.8,<3.14"
dependencies = ["PySide6==6.11.0", "pyserial==3.5"]

[tool.py_upper]
entry = "main.py"                 # app/src 下的入口文件

[tool.py_upper.app]
name = "MyApp"                    # 最终应用名（.app / 可执行文件 / 窗口标题，见 §10.2）
identifier = "com.example.pyupper" # macOS bundle identifier

[tool.py_upper.runtime]
provider = "pbs"                  # pbs | local
python = "3.13.5"                # 目标 Python 精确版本（同时有 Windows 官方 embed ZIP 与 7 个 PBS SDK）
# provider = "local" 时改为：
# runtime = "runtimes/{target}/{python}"
# sdk     = "build/local-sdk/{target}/{python}"

[tool.py_upper.pbs]
# release = "20230507"            # 可选，固定 PBS release；省略则自动解析

[tool.py_upper.dependencies]
# find_links = ["wheelhouse"]     # 离线/内网 wheel 目录
# no_index = true
# index_url = "https://pypi.tuna.tsinghua.edu.cn/simple"
# extra_index_urls = ["..."]
manylinux = ["manylinux_2_39", "manylinux_2_34"]  # Linux 兼容 baseline，从新到旧
timeout = 120                     # pip socket 超时（秒）
retries = 10                      # pip 重试次数

[tool.py_upper.native]
exclude = [                       # 丢弃无法在包内满足依赖的可选原生组件
  "PySide6/Qt/plugins/sqldrivers/*qsqlodbc*",
]

[tool.py_upper.cython]
include = ["*"]                   # 相对 app/src 的模块名 glob
exclude = []

[tool.py_upper.build]
jobs = 0                          # 并行编译任务数；0 = 每 CPU 一个
incremental = true                # 只重编译变化的模块；false = 每次全量

[tool.py_upper.optimize]
profile = "safe"                  # safe | aggressive
remove_python_caches = true       # 删 __pycache__ / *.pyc
remove_tests = false              # aggressive 时自动 true
remove_docs = false
strip_native = false              # strip 原生二进制（macOS 会重新 ad-hoc 签名）
remove_runtime_pip = true         # 从打包副本里删 pip/ensurepip
rewrite_rpath = false             # Linux：把 RPATH 改写成 $ORIGIN（需要 patchelf）
qt = "imports"                    # all | imports | ["QtWidgets", ...]

[tool.py_upper.windows]
architectures = ["x86", "x86_64", "arm64"]
[tool.py_upper.macos]
architectures = ["arm64", "x86_64"]
[tool.py_upper.linux]
architectures = ["x86_64", "arm64"]
```

Windows + PBS：发布 runtime 来自 python.org 官方 embeddable ZIP；PBS full SDK 只用于编译、目标 pip 解析和 Qt import 闭包，不会进入 dist。编译侧读的一切（`Python.h`、`libs/python3XX.lib`）都只从 SDK 取：发布 runtime 是嵌入形状，没有 `libs/`，也不是链接输入。官方 embed ZIP 的 `_pth` 和根目录 `.pyd` 布局由 launcher 处理，打包后的 launcher smoke 验证依赖 import。精确版本必须同时存在官方 ZIP 与 PBS SDK（例如 3.13.5；3.11.13 无官方 ZIP）。

### 环境变量

| 变量 | 作用 |
|---|---|
| `PY_UPPER_PYTHON` / `PYSTAND_PYTHON` | 指定 Build Tool 解释器 |
| `PY_UPPER_TRACEBACK=1` | 构建失败时打印完整 traceback |
| `PY_UPPER_LOCKED=1` | 等价 `--locked` |
| `PY_UPPER_HTTP_TIMEOUT` / `PY_UPPER_HTTP_RETRIES` | PBS 下载超时/重试 |
| `PY_UPPER_PIP_TIMEOUT` / `PY_UPPER_PIP_RETRIES` | pip 传输超时/重试 |
| `PY_UPPER_HEADLESS=1` | 应用以 offscreen 方式建 Qt 窗口后退出（CI/smoke 用） |
| `PY_UPPER_NO_DIALOG=1` | launcher 不弹原生错误对话框，失败只走 stderr + 退出码（CI/smoke 必须设） |
| `PY_UPPER_LAUNCHER_CONSOLE=1` | Windows：构建控制台子系统 launcher，用于调试（等价 `-DPY_UPPER_LAUNCHER_CONSOLE=ON`） |
| `PY_UPPER_E2E=1` | 打开本地端到端测试 |

launcher 运行时还会向应用导出 `PY_UPPER_HOME` / `PY_UPPER_RUNTIME` / `PY_UPPER_SITE_PACKAGES` / `PY_UPPER_SCRIPT` / `PY_UPPER_EXECUTABLE`，见 §10.2。

---

## 6. 日常开发

### 6.1 直接开发，不打包

`app/src` 就是普通 Python 包根，日常开发不需要打包：

```bash
cd app
PYTHONPATH=src python -c "from application import main; main()"
# 或
PYTHONPATH=src python src/main.py
```

注意：直接运行 `main()` 会尝试打开 Qt 窗口；CI/无显示环境加 `PY_UPPER_HEADLESS=1`。

还没装 Qt 时也能看到装配跑通：`import application` 不加载任何 binding（见 §9.4），
`PYTHONPATH=src python -c "from application import run_application; run_application()"` 只打印问候语，不碰 Qt。

### 6.2 测试

```bash
python -m pytest app/tests -q                 # 单元 + 兼容性测试
PY_UPPER_E2E=1 python -m pytest app/tests -q  # 额外跑真实端到端（较慢，需 C 编译器）
```

单元测试用 Build Tool 的解释器跑；`test_e2e_local.py` 默认跳过。

### 6.3 VS Code

`.vscode/` 已内置：

- `launch.json`
  - **Python: app/src/main.py**：直接调试应用（`PYTHONPATH=app/src`，cwd=`app`）
  - **py_upper: packaged app**：执行 `tools/build.py --run`，构建并启动打包后的应用
- `tasks.json`
  - **py_upper: build** / **py_upper: run packaged app**
- `settings.json`：pytest 与 `app/src` 分析路径，并把 `build/`、`dist/`、`runtimes/` 排除出文件监视与搜索

`settings.json` 里的 `files.watcherExclude` / `search.exclude` 不是可选的整理项，删掉它们本机构建会重新变得不稳定：

1. Python 扩展会为每个工作区注册 `**/python` 的文件监视（`createPythonWatcher`）。构建过程中 runtime 的解释器一被创建或改写，扩展就把这个路径交给 `python-env-tools` 去 resolve。
2. resolve 会**真的执行**那个解释器（`python -c "import json, sys; ..."`），解释器启动时把 bytecode 写进自己所在的 runtime。
3. 构建随后清理这棵树，就撞上这些新写入：`FileNotFoundError: .../utf_8.cpython-311.pyc.<随机数>` 或 `OSError: [Errno 66] Directory not empty: .../__pycache__`。

`runtimes/` 同理：它是目标 runtime 缓存，构建会执行其中的解释器。`files.watcherExclude` 是唯一能挡住这条链路的东西（`search.exclude` 只影响搜索与扩展的 `findFiles` 扫描），`app/tests/test_build_tool_compat.py` 有用例锁住这三项。

调试建议：

- 想调试 **打包结果**：用 "py_upper: packaged app"；想调试 **源码**：用第一个配置。
- 调试 Build Tool 自身：给 `tools/build.py` 打断点，`PY_UPPER_TRACEBACK=1` 让异常完整抛出。
- 打包后的 App 里源码已被 Cython 化，无法直接断点；要断点就调试源码配置。

### 6.4 增量编译

构建工具把每个应用模块的 Cython 产物与目标扩展缓存在 `build/cache/`，并按内容判定是否需要重建：

- 生成的 `.c` 由**源码文本 + Cython 版本**决定；
- 目标扩展由**完整的编译命令行**（编译器、架构、ABI 后缀、SDK include 目录、优化开关）与**被编译的 `.c` 内容**决定。

所以只有真正变化的模块会重新 Cython 化并编译，其余直接从缓存复制。两层是链式的：改一个 `.py` 只会让它的 `.c` 和它自己的扩展失效，删掉或改名的模块不会留下会被打包的旧产物（staging 只处理当前源码）。

Cython 与编译器调用都在线程池里并发执行，并行度由 `[tool.py_upper.build].jobs` 控制（默认 `0` = 每 CPU 一个任务）。

缓存是纯产物，随应用增长：`python tools/build.py --clean` 会连同 `build/` 一起删掉。需要强制全量重编译时用 `incremental = false` 或先 `--clean`。

---

## 7. 命令参考

```bash
python tools/build.py                 # 完整构建（默认）
python tools/build.py --run           # 构建并运行（仅宿主平台目标）
python tools/build.py --verify        # 只做静态校验（可重复执行）
python tools/build.py --doctor        # 打印宿主/目标/工具链信息
python tools/build.py --lock          # 解析并写入 py_upper.lock.json
python tools/build.py --locked        # 只使用锁文件与本地缓存构建
python tools/build.py --release       # 构建 + 签名（+ macOS 公证）
python tools/build.py --clean         # 清空 build/ 与 dist/
python tools/build.py --target linux-arm64     # 指定目标
```

`--run` 只允许运行宿主平台目标；交叉目标可以 build/verify。

锁文件：`py_upper.lock.json` 记录 runtime/SDK 与 wheel 集合及校验值。`--locked` 不重新解析依赖，适合 CI 与可复现发布。

---

## 8. 依赖管理

### 8.1 解析语义（重要）

pip 的 `--platform` / `--python-version` **只影响 wheel 兼容标签**，环境标记（`sys_platform`、`python_full_version` 等）永远按**运行 pip 的那个解释器**求值。

因此 py_upper 在**宿主可执行目标 runtime** 时，直接用**目标解释器**解析依赖：

```
目标 3.10 + 宿主 3.11 时，bleak 需要 async-timeout（marker: python_full_version < "3.11"）
→ 用目标解释器解析才能正确带上它
```

交叉目标无法执行目标解释器，退回宿主解释器 + 显式标签参数；此时平台/版本标记按宿主求值，带平台标记的依赖可能解析失败。

### 8.2 Linux 兼容基线

`manylinux` 接受字符串或列表（从新到旧）。列表是必要的：PySide6 的 x86_64 wheel 是 `manylinux_2_34`，aarch64 是 `manylinux_2_39`。列表同时决定发布包的最低 glibc。

### 8.3 离线 / 内网

```toml
[tool.py_upper.dependencies]
find_links = ["wheelhouse"]
no_index = true
```

### 8.4 原生依赖闭包

打包时会扫描 site-packages 里的原生文件，递归解析依赖并复制缺失库，macOS 上把 install name 改写成 `@loader_path/...` 并重新 ad-hoc 签名。找不到的依赖是**硬错误**（不会静默产出坏包），可用 `[tool.py_upper.native].exclude` 显式丢弃可选组件。

---

## 9. Qt 应用

### 9.1 引用 Qt：只 import `utils.qt_compat`

应用代码不直接 import binding，而是从这个 shim 拿模块对象：

```python
from utils.qt_compat import QtCore, QtWidgets, Signal

class Window(QtWidgets.QMainWindow):
    closed = Signal()
```

`utils.qt_compat` 是**模块代理**，不是函数表：`QtWidgets`、`QtCore`、`QtNetwork` 这类名字在被访问时才解析成当前 binding 的同名子模块，返回的是真正的模块对象，所以 IDE 能补全 `QtWidgets.QMainWindow`、能跳转定义、类型检查读 `TYPE_CHECKING` 分支里的 PySide6 stub（开发环境装 PySide6 就够）。运行期按 `PySide6` → `PyQt6` → `PySide2` → `PyQt5` 取第一个可用的，并在进程内固定——同一个解释器加载两个 binding 会直接崩，所以这个选择不做动态切换。

规则：

1. import `utils.qt_compat` 本身不加载任何 binding，没装 Qt 的环境里也能 import、lint、Cython 编译；
2. 拼错的子模块（`QtWidget`）在 import 那一行就报 `ImportError`，不会拖到用的时候；
3. `Signal` / `Slot` 由这里转出：PySide 侧是 `Signal`/`Slot`，PyQt 侧是 `pyqtSignal`/`pyqtSlot`，应用代码只写一种拼法；
4. 整个 `app/src` 里只有 `utils/qt_compat.py` 允许出现 binding 名，`app/tests/test_qt_compat.py` 用 ast 不变式锁住这条；换 binding 只改 `app/pyproject.toml` 的依赖，应用代码与 Qt 剪枝都跟着走；
5. 应用自己的模块也别在模块级 import Qt：`mvvm/views/*.py` 和 `mvvm/mvvm_bootstrap.py` 把 Qt 名字放在真正建窗口的那个函数里（见 §9.4）。模块级 import 会让「import 应用入口」变成「必须装 Qt」——pytest、pyflakes、Cython 编译全被拖进来，而它们本来不需要 binding。

### 9.2 窗口与 headless

应用入口建议：

```python
def main() -> int:
    run_application()      # 打印/初始化
    return run_gui()       # 打开 Qt 主窗口
```

`run_gui()` 在 `PY_UPPER_HEADLESS=1` 时用 offscreen 平台建同一个窗口、跑一次事件循环后退出并打印 `GUI OK offscreen`。CI 与 smoke 靠这个在不接显示器的环境里验证 Qt 真的可用。

### 9.3 按 import 裁剪（体积关键）

PySide6 的 wheel 会带上整个 Qt，`QtWebEngineCore` 一个约 450 MB。开启：

```toml
[tool.py_upper.optimize]
qt = "imports"
```

规则：

1. 用 `ast` 读 `app/src` 每个文件的 import 语句（多行括号 import 一样算；函数内的 import 也在遍历范围里，所以 `mvvm/views` 在 `__init__` 里 import 的 Qt 名字不会被漏掉）：直接写 binding 前缀（`PySide6.QtWidgets`）和走 shim（`from utils.qt_compat import QtWidgets`）归到同一个 Qt 模块名，具体是哪个 binding 由 site-packages 里实际存在的那个决定；解析不了的源文件直接报错，不静默降级；
2. 用目标解释器导入并解析**传递闭包**；
3. 删除未用到的 wrapper 模块、Qt 库、插件类别、QML 树、shiboken QML 辅助库、Qt 工具二进制、工具翻译；
4. 插件按**依赖的 Qt 模块**判定：需要额外 Qt 模块的插件（如虚拟键盘输入法插件拉 QtQml/QtQuick、`libqpdf` 拉 QtPdf）会被删除；**平台插件类别永不裁剪**（删了应用起不来）；SVG 图标插件走例外保留。

实测（PySide6 6.11.0，macOS arm64）：staging 的 `site-packages` **1.2 GB → 109 MB**（PySide6 本体 1.2 GB → 105 MB），打包后 `dist/MyApp.app` 100 MB、其中 PySide6 58 MB；再开 `strip_native = true` 原生库还能更小。

限制：动态加载（`importlib`、QML 内 import）静态看不到，这类应用请显式写模块列表；交叉目标无法执行目标解释器，会保留完整 Qt。

### 9.4 MVVM 应用骨架

`app/src/mvvm/` 是一个能跑的最小 MVVM 骨架，依赖方向单向：View → ViewModel → Model/Service，反向靠属性的变更通知回来。

```text
app/src/
├── main.py                  # 入口，只把控制权交给 application.py
├── application.py           # ★ 装配处：唯一同时认识下面四层的文件
├── utils/qt_compat.py       # Qt 名字的唯一入口（见 §9.1）；utils/ 也是 launcher 契约的家
└── mvvm/
    ├── core/                # ObservableProperty / RelayCommand / 工厂 / ViewModel 基类
    ├── models/              # 数据与业务规则
    ├── view_models/         # 界面状态与命令
    ├── views/               # 渲染与绑定
    └── services/            # 被注入的能力（通信、仓库、后端）
```

规则：

1. 四层互相不 import 具体实现，`application.py` 是唯一同时 import models / view_models / views / services 的文件；换实现只改这一个文件；
2. `models/`、`view_models/`、`services/` 不 import Qt——它们只持有数据和命令，界面能不能显示与它们无关；
3. Qt 名字在真正建窗口的地方才 import（`MainWindow.__init__`、`create_application()`），所以 `import application` 不加载任何 binding；
4. 「服务」是注入进去的能力，不是全局单例：`GreetingService` 由装配处注册进容器，再传给 ViewModel；
5. 一个 ViewModel 可以配多个 View。绑的是属性与命令，不是控件——同一份状态换一个窗口照样能用。

容器 API（`mvvm/mvvm_application.py`）：

```python
application.register_service("greeting", GreetingService())
application.resolve_service(GreetingService)          # 按类型取；按名字取用 get_service()
application.register_viewmodel("main", MainViewModel(...))
application.get_viewmodel("main")
application.register_view("main", MainWindow(...))
application.get_view("main")
application.start()   # 置位运行标记 + 依次 show 所有已注册 View
application.stop()    # close 所有 View → dispose 所有 ViewModel → 清空注册表
```

`MVVMQtApplication`（`mvvm/mvvm_bootstrap.py`）只固定顺序：`setup()` 建容器 → `initialize_viewmodels()` → `initialize_views()` → `start()`，`run()` 再进 Qt 事件循环。子类只写 `initialize_viewmodels()` 和 `initialize_views()` 这两个钩子。

绑定 API（`mvvm/views/base_view.py`，View 继承 `ViewBase`）：

```python
self.bind_property("message", label, "setText")       # 属性 → 控件：setter 方法被调用，普通属性才 setattr
self.bind_command("show_about", button)               # 命令 → 控件事件（默认 clicked）
binding = self.bind_property("title", line_edit, "setText", getter_name="text")
binding.update_viewmodel_from_ui()                    # 反向：UI → ViewModel
```

双向绑定必须显式给 `getter_name`，缺了直接抛 `ValueError`：Qt 的 setter 与 getter 不同形（`setText`/`text`、`setWindowTitle`/`windowTitle`），从 setter 反推 getter 猜错了就是静默读错值。

示例：桌面上的「关于 MVVM 架构」按钮。它是这个骨架的参考实现，整条链路没有一处绕过 ViewModel：

```text
MainWindow 的按钮 ─CommandBinding→ MainViewModel.show_about_command
                                        └─ about_visible = True
                                                │（ObservableProperty 通知）
MainWindow._on_viewmodel_property_changed ─────→ AboutWindow.show()
AboutWindow 的「关闭」按钮 ─CommandBinding→ MainViewModel.hide_about_command → about_visible = False → AboutWindow.close()
```

`about_visible` 是唯一真相源：主窗口和「关于」窗口都不自己存可见状态，谁都不需要知道对方存在。主窗口拿到的「关于」窗口是构造参数（`MainWindow(viewmodel, about_window)`），不是自己 new 的。

---

## 10. 启动器契约（launcher）

`launcher/` 是一个**不链接 libpython** 的 C++ 可执行文件：它在运行时用 `dlopen`/`LoadLibraryEx` 从 bundled runtime 里解析 CPython 符号（`PyConfig_InitIsolatedConfig` 等），因此同一份 launcher 源码可以为任意目标 Python 版本构建，换 runtime 不需要重新链接。

### 10.1 入口解析

launcher 用**自身文件名**（stem）在 bundle 根目录按顺序查找入口：

1. `<stem>.int`（文档化布局）
2. `<stem>.py`
3. `<stem>.pyw`
4. `_py_upper_static.int`（**改名安全兜底**）

打包时 `<App>.int` 与 `_py_upper_static.int` 都会写出，内容相同。把可执行文件改名后，兜底入口仍能让应用启动；`--verify` 里对应 `rename-safe application entry`。`PY_UPPER_SMOKE=1` 时改走 `<stem>.smoke.int`。

### 10.2 导出给应用的变量

| 变量 | 值 |
|---|---|
| `PY_UPPER_HOME` | 入口所在目录（macOS：`Contents/Resources`） |
| `PY_UPPER_RUNTIME` | bundled runtime 根 |
| `PY_UPPER_SITE_PACKAGES` | `site-packages` |
| `PY_UPPER_SCRIPT` | 实际选中的入口文件 |
| `PY_UPPER_EXECUTABLE` | launcher 绝对路径 |
| `PY_UPPER_APP_NAME` | 打包时编译进 launcher 的应用名（改名不会变） |

应用侧取资源目录用 `utils.paths.resource_root()`：优先 `PY_UPPER_HOME/resources`，没有该变量时才回退到"数父目录"。数父目录只对某一天的目录布局成立，launcher 才知道真实答案。`app/resources` 是**可选**的：目录不存在时产物里就没有 `resources/`，调用方要按"可能不存在"处理。

应用名同理只有一个来源：`app/pyproject.toml` 的 `[tool.py_upper.app].name`。打包链由它派生 `.app`/可执行文件/入口脚本/`Info.plist`/Windows 版本资源的名字，并把它编译进 launcher；运行时应用统一调 `utils.paths.app_name()` 读取（先看 `PY_UPPER_APP_NAME`，开发态没有 launcher 时回读同一个 `pyproject.toml`）。**不要**从 `PY_UPPER_EXECUTABLE` 或 `argv[0]` 推名字——可执行文件是允许改名的，也不要在应用代码里硬编码第二份。

### 10.3 失败可见性

- 致命错误（入口缺失、runtime 加载失败、解释器初始化失败、应用抛未捕获异常）先写 stderr，再按需弹**原生对话框**：Windows `MessageBoxW`、macOS `CFUserNotification`、Linux `zenity`（退回 `xmessage`）。macOS 的对话框有 120 s 超时，无人值守不会挂死。
- 已经能看到 stderr 时不弹窗：设置了 `PY_UPPER_NO_DIALOG`，或 stdout/stderr 是 tty。**CI 与 smoke 必须设 `PY_UPPER_NO_DIALOG=1`**。
- 应用抛未捕获异常时，bootstrap 先把 traceback 写进 `TMPDIR/py_upper-<pid>.log`；`Py_FinalizeEx` 之后 launcher 检查该文件并把它作为致命错误上报，然后删除。双击启动（没有终端）也能看到 traceback，且不会在包里留下垃圾文件。
- 退出码：1 无 argv / 未捕获异常，3 入口缺失，4 libpython 加载失败，5 符号缺失，6 配置失败，7 初始化失败，8 `PyRun_SimpleString` 缺失，9 launcher 内部异常。

### 10.4 平台差异

- **Windows**：默认编译成 GUI 子系统（`WIN32_EXECUTABLE`），双击不弹控制台；从 cmd/PowerShell/CI 启动时用 `AttachConsole(ATTACH_PARENT_PROCESS)` 接管父控制台，**已有的重定向优先**（`> run.log` 不会被抢走）。入口是 `wWinMain`，宽命令行统一转 UTF-8 后再处理，非 ASCII 安装路径不会乱码。链接静态 CRT（`/MT`），目标机不需要 VC++ 运行库。调试时用 `PY_UPPER_LAUNCHER_CONSOLE=1` 构建控制台版本。runtime 目录里的 `python3.dll` 是稳定 ABI 转发层，只再导出受限 API，`PyConfig_*` 不在其中；launcher 会把该目录下所有 `python*.dll` 都加载（abi3 扩展按名字 import 转发层，必须留在已加载模块表里），入口点则从真正导出它们的那一个（`python3XX.dll`）解析。官方 embeddable runtime 没有 `DLLs/`，标准扩展（`_socket.pyd`、`_ssl.pyd` 等）直接躺在 `python3XX.dll` 旁边，所以 launcher 在 `DLLs/` 不存在时把 runtime 根目录当作扩展目录。
- **macOS**：入口在 `Contents/MacOS`，资源 / runtime / site-packages 都在 `Contents/Resources`；链接 CoreFoundation 以使用 `CFUserNotification`。
- **Linux**：单目录布局，`libpython3.x.so` 以 `RTLD_GLOBAL` 加载，保证后续导入的扩展能解析 CPython 符号。

### 10.5 Windows 版本资源

打包时生成 `py_upper_resource.rc`（`VERSIONINFO`：FileDescription / FileVersion / ProductName / ProductVersion / OriginalFilename）并交给 CMake 编译，可执行文件属性里能看到产品名与版本。**当前不嵌入图标**：仓库里没有图标资源，`Info.plist` 也没有 `CFBundleIconFile`；需要时新建 `app/resources`（可选目录，存在就会被打进包）把 `.ico`/`.icns` 放进去，再扩展这条链路。

---

## 11. 验证与 CI

两道门：

1. **静态校验**（`--verify`）：文件存在、架构匹配、扩展 ABI 后缀、依赖清单、包布局。可重复执行（幂等）。
2. **真 import smoke**（`--run` 之前自动执行）：
   - 用 bundled Python 导入入口模块 + 所有直接声明的第三方依赖；
   - 用打包好的 launcher 再导入一次（`PY_UPPER_SMOKE=1`）；
   - 最后才真正运行应用。

launcher 相关步骤一律带 `PY_UPPER_NO_DIALOG=1`：CI 里一次失败必须是退出码，而不是一个没人能点的对话框。

CI（`.github/workflows/validate.yml`）在**每个原生平台**都跑：

```
单元测试 → 本地端到端构建（第三方 wheel、应用自带 native 库、lock/--locked）
→ doctor → 完整 PBS 构建 → 集成运行（headless，断言 GUI OK）
```

原生集成 job 从模板生成 `app/pyproject.toml` 时会把仓库自身示例应用的依赖集合填进 `[project] dependencies`（模板里是空列表），否则集成运行没有 PySide6 可用；纯测试与聚合 job 只需要文件存在，直接复制模板。E2E 夹具同样自己重写这一段依赖。

触发范围只有 develop：`push` 与 `pull_request` 都带 `branches: [develop]`（`pull_request` 过滤的是 base 分支），`workflow_dispatch` 保留给失败重跑。`feature_*` / `fix_*` 临时分支上的中间提交不跑矩阵——它们的验证由合并后的 develop 承担，一次功能开发不会在临时分支上白烧十几轮全平台 job。确实要单独验证某个分支时用 `gh workflow run validate.yml --ref <branch>` 手动触发。

---

## 12. 发布

```bash
python tools/build.py --release --identity "Developer ID Application: ..." --notary-profile <profile>
```

- **macOS**：对嵌套 dylib/so 与应用本体签名，`--notary-profile` 时提交公证并 staple。
- **Windows**：需要 `signtool` 与 `PY_UPPER_SIGN_CERT`。
- **Linux**：交给发行版打包/签名体系。

签名顺序由工具保证：嵌套代码由内向外，launcher 最后（否则 bundle 封签会失效）。

---

## 13. 排错手册

| 症状 | 根因 | 处理 |
|---|---|---|
| `Could not find the Qt platform plugin "cocoa"` / 窗口打不开 | 平台插件被裁剪或缺失 | 升级到 ≥0.17.3；平台插件类别现在永不裁剪。临时可把 `qt` 设为 `"all"` |
| `symbol not found: _PyArg_ValidateKeywordArguments` | launcher 用 `RTLD_LOCAL` 加载 libpython | ≥0.16.15 已改为 `RTLD_GLOBAL` |
| `No module named 'main'` | 扩展后缀不是 CPython 识别的形式 | 应为 `.cpython-310-darwin.so`（macOS）/`.cp310-win_amd64.pyd` |
| `import zlib` 失败 / Cython 字符串压缩报错 | 搜索路径缺 `lib-dynload`（Unix）或 `DLLs`（Windows） | ≥0.17.1 已加入 launcher 搜索路径 |
| `PermissionError: ... python3.10` | runtime 拷贝丢了可执行位 | ≥0.17.1 拷贝时保留执行位 |
| `Errno 1: Operation not permitted` 打包时 | 回放源文件权限/元数据 | ≥0.16.10 只拷内容 + 执行位 |
| `Unresolved native dependencies` | 原生依赖闭包里有找不到的库 | 看清单；可选组件用 `[tool.py_upper.native].exclude`；自带库放 `app/src` 会被自动纳入 |
| Linux 上 `Unresolved native dependencies` 里全是 `libGL` / `libEGL` / `libxcb*` / `libxkbcommon` / `libwayland*` / `libwebp*` / `libtiff` | Qt 平台插件无条件链接宿主桌面栈，这些库属于目标机 | 已在 `deps.py` 的 `LINUX_HOST_LIBRARIES` 白名单，不再算未解析；目标机需装运行库（Debian/Ubuntu：`libgl1 libegl1 libxkbcommon0 libwayland-client0 libtiff6 libwebp7`） |
| `@rpath/xxx.dylib` 被当成依赖 | 把 `LC_ID_DYLIB` 当 `LC_LOAD_DYLIB` | ≥0.16.13 已区分 identity 与依赖 |
| `No PBS runtime metadata for exact Python X` | 元数据缓存被污染或版本不存在 | 删除 `.cache/pbs/uv-download-metadata.json` 重试；确认 PBS 有该精确版本 |
| 构建卡在 native dependency 很久 | 依赖闭包退化成全树扫描 | ≥0.17.1 已改为一次建索引 |
| `codesign` 报 `code object is not signed at all` | 嵌套 bundle 未按由内向外签名 | ≥0.17.2 按路径深度倒序签名 |
| 应用体积 1 GB+ | 完整 Qt 被打包 | 设 `qt = "imports"` |
| 构建日志出现 `app/src does not reference a Qt binding, keeping the full Qt payload` | 剪枝没在 `app/src` 找到任何 Qt import（binding 前缀或 `utils.qt_compat` 前缀），于是整块保留 | ≥0.17.9 用 `ast` 读 import 并认识 `utils.qt_compat`；仍有这行说明应用确实没 import Qt，或者 `qt` 设成了 `"all"` |
| pip 下载大 wheel 超时 | 默认 15s socket 超时 | 设 `timeout`/`retries` 或 `PY_UPPER_PIP_TIMEOUT` |
| VS Code 调试报 `No module named 'Cython'` | 探测 Cython 时抛预期异常 | ≥0.16.8 改用 `importlib.metadata`，不会停在异常上 |
| 双击打包好的应用"没反应" | 入口/runtime/初始化失败，而 GUI 子系统没有控制台 | ≥0.17.5 会弹原生错误对话框显示原因；自动化场景设 `PY_UPPER_NO_DIALOG=1` 只取退出码 |
| 应用启动即退出，看不到 traceback | 未捕获异常被解释器写到不存在的 stderr | ≥0.17.5 bootstrap 先落 `TMPDIR/py_upper-<pid>.log`，launcher 在 `Py_FinalizeEx` 后据此上报；双击时会弹对话框 |
| 把可执行文件改名后起不来 | 入口名跟随可执行文件名 | ≥0.17.5 打包时同时写 `_py_upper_static.int` 兜底 |
| Windows 上从 cmd 运行看不到输出 | GUI 子系统没有自己的控制台 | ≥0.17.5 自动 `AttachConsole` 到父控制台（已有重定向优先）；要无条件控制台用 `PY_UPPER_LAUNCHER_CONSOLE=1` 重新构建 |
| Windows 目标机报缺 `VCRUNTIME140.dll` | launcher 动态链接 CRT | ≥0.17.5 launcher 使用静态 CRT（`/MT`） |
| 非 ASCII 安装路径下应用起不来 | 窄字符路径按 ANSI 代码页解释 | ≥0.17.5 入口/路径全程 UTF-8，`std::filesystem` 走 `u8path` |
| 改了代码但该模块没有重新编译 | 增量缓存命中（源码文本与上次一致） | 确认文件内容真的变了；强制全量用 `[tool.py_upper.build].incremental = false` 或 `--clean` |
| Windows 编译扩展报 `Import library of target Python ... not found under the SDK tree` | 导库是编译器输入，只从 SDK 取；发布 runtime 是嵌入形状，没有 `libs/` | ≥0.17.8 已从 SDK 取；SDK 缓存不完整时删 `.cache/pbs-sdk/<target>/<python>` 重新下载 |

调试技巧：`PY_UPPER_TRACEBACK=1 python tools/build.py`；`python tools/build.py --doctor` 看宿主/目标/工具链；`--verify` 可反复执行定位到具体失败项。

---

## 14. 版本与提交约定

- 每个版本一个提交 + 一个 tag（`v0.17.x`），提交信息包含 Problem / Root cause / Changes / Verification / Impact。
- `CHANGELOG.md` 记录面向使用者的变更与限制，`HISTORY.md` 记录简版演进。
- 版本号在 `app/pyproject.toml` 的 `[project].version`；测试从配置读取，不再硬编码。
- `runtimes/`、`build/`、`dist/`、`.cache/` 均不入库。

---

## 15. 模块职责

| 模块 | 职责 |
|---|---|
| `config.py` | 目标契约、配置解析、平台标签、pip 传输参数 |
| `toolchain.py` | 宿主工具链解析（clang/gcc/MSVC，以及无 VS 宿主上的 Zig 交叉路径） |
| `pbs.py` | PBS 解析：release 元数据、资产匹配、runtime/SDK 获取与校验 |
| `runtime.py` | 目标 runtime：provider 分发（pbs/local）、获取、release 安全裁剪 |
| `third_party.py` | wheel 解析/安装、site 优化、依赖清单 |
| `qt_prune.py` | 按 import 裁剪 Qt 负载（用 `ast` 读语句，认识 binding 前缀与 `utils.qt_compat`） |
| `python_build.py` | Cython 化、目标扩展编译（Unix/Windows）、增量产物缓存、并行调度、staging |
| `native/inspect.py` | 二进制格式与架构识别 |
| `native/deps.py` | 依赖解析（otool/readelf/dumpbin + 索引/缓存） |
| `native/bundle.py` | 依赖闭包、install name 重写、ad-hoc 签名 |
| `package.py` | launcher 构建（含 Windows 版本资源）、入口脚本、产物组装、原生排除、发布清单 |
| `verify.py` | 静态校验 + 目标 runtime/launcher 真 import smoke |
| `launcher/` | C++ launcher 实现（CMake 由 `package.build_launcher` 驱动） |
| `lock.py` / `manifest.py` | 锁文件与 runtime manifest（可复现性） |
| `fs.py` / `net.py` / `compat.py` | 文件拷贝与哈希、HTTP 重试下载、tomllib 兼容 |
