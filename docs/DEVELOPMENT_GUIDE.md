# py_upper 开发使用指南

本文是 `py_upper` 的完整开发与使用手册：从环境准备、日常开发、打包发布，到 Qt 应用处理、依赖管理和排错。README 讲的是"是什么"，本文讲的是"怎么用、为什么这么设计、出问题怎么查"。

当前稳定线：**0.17.3**。

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
[2/6] Third-party wheels   解析并安装目标平台 wheel
[3/6] Target launcher      CMake + Ninja 编译 C++ 启动器
[4/6] Package              复制运行时、site-packages、资源 + 原生依赖闭包 + 签名
[5/6] Static verification  文件、架构、ABI 后缀、依赖清单
[6/6] Runtime smoke        用 bundled Python 真 import + 启动器 smoke + 可选运行
```

---

## 2. 环境准备

### 2.1 宿主平台与工具

| 目标 | 宿主要求 | 额外工具 |
|---|---|---|
| macOS（arm64 / x86_64） | macOS | Xcode Command Line Tools（`clang`）、`cmake`、`ninja` |
| Windows（x86 / x86_64 / arm64） | Windows | Visual Studio Build Tools（含 C++）、`cmake` |
| Linux（x86_64 / arm64） | Linux | `gcc`/`g++`、`cmake`、`ninja`、`binutils`（可选 `patchelf`） |

交叉目标（例如在 macOS 上打 Linux 包）可以 build/verify，但不会冒充在当前机器运行；带平台标记的第三方依赖需要目标平台的宿主，见 §8.1。

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

PBS runtime/SDK 与 PyPI wheel 都走 HTTPS。国内网络建议给本地构建设镜像（不影响 CI）：

```bash
export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
```

pip 的下载缓存放在 `.cache/pip`，重复构建不会重下几百 MB 的 wheel。

---

## 3. 快速开始

```bash
# 1. 写代码：app/src 就是应用源码
app/src/
├── main.py            # 入口（由 [tool.py_upper].entry 指定）
├── core/app.py
└── ...

# 2. 声明依赖与目标
#    app/pyproject.toml
#    [project] dependencies = ["PySide6==6.11.0"]
#    [tool.py_upper.runtime] provider = "pbs"  python = "3.10.11"

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
    └── resources/                # app/resources 的内容

dist/MyApp/                       # Windows / Linux
├── MyApp(.exe)                   # launcher
├── MyApp.int
├── runtime/
├── site-packages/
└── resources/
```

---

## 4. 目录结构

```text
py_upper/
├── app/
│   ├── pyproject.toml        # 唯一配置入口
│   ├── src/                  # ★ 应用源码（直接是包根）
│   ├── tests/                # 应用测试 + 构建链测试
│   └── resources/            # 随包资源
├── launcher/                 # C++ 启动器（CMake）
├── tools/
│   ├── build.py              # ★ 唯一公共构建入口
│   └── py_upper/             # 构建工具实现（见 §14）
├── runtimes/                 # 本地 runtime 缓存（git 忽略）
├── .cache/                   # PBS / pip 缓存（git 忽略）
├── build/                    # 中间产物（git 忽略）
└── dist/                     # 最终产物（git 忽略）
```

---

## 5. 配置参考（`app/pyproject.toml`）

```toml
[project]
name = "py_upper"
version = "0.17.2"
requires-python = ">=3.8,<3.14"
dependencies = ["PySide6==6.11.0", "pyserial==3.5"]

[tool.py_upper]
entry = "main.py"                 # app/src 下的入口文件

[tool.py_upper.app]
name = "MyApp"                    # 最终应用名（.app / 可执行文件名）
identifier = "com.example.pyupper" # macOS bundle identifier

[tool.py_upper.runtime]
provider = "pbs"                  # pbs | local
python = "3.10.11"                # 目标 Python 精确版本
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

### 环境变量

| 变量 | 作用 |
|---|---|
| `PY_UPPER_PYTHON` / `PYSTAND_PYTHON` | 指定 Build Tool 解释器 |
| `PY_UPPER_TRACEBACK=1` | 构建失败时打印完整 traceback |
| `PY_UPPER_LOCKED=1` | 等价 `--locked` |
| `PY_UPPER_HTTP_TIMEOUT` / `PY_UPPER_HTTP_RETRIES` | PBS 下载超时/重试 |
| `PY_UPPER_PIP_TIMEOUT` / `PY_UPPER_PIP_RETRIES` | pip 传输超时/重试 |
| `PY_UPPER_HEADLESS=1` | 应用以 offscreen 方式建 Qt 窗口后退出（CI/smoke 用） |
| `PY_UPPER_E2E=1` | 打开本地端到端测试 |

---

## 6. 日常开发

### 6.1 直接开发，不打包

`app/src` 就是普通 Python 包根，日常开发不需要打包：

```bash
cd app
PYTHONPATH=src python -c "from core.app import main; main()"
# 或
PYTHONPATH=src python src/main.py
```

注意：直接运行 `main()` 会尝试打开 Qt 窗口；CI/无显示环境加 `PY_UPPER_HEADLESS=1`。

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
- `settings.json`：pytest 与 `app/src` 分析路径

调试建议：

- 想调试 **打包结果**：用 "py_upper: packaged app"；想调试 **源码**：用第一个配置。
- 调试 Build Tool 自身：给 `tools/build.py` 打断点，`PY_UPPER_TRACEBACK=1` 让异常完整抛出。
- 打包后的 App 里源码已被 Cython 化，无法直接断点；要断点就调试源码配置。

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

### 9.1 窗口与 headless

应用入口建议：

```python
def main() -> int:
    run_application()      # 打印/初始化
    return run_gui()       # 打开 Qt 主窗口
```

`run_gui()` 在 `PY_UPPER_HEADLESS=1` 时用 offscreen 平台建同一个窗口、跑一次事件循环后退出并打印 `GUI OK offscreen`。CI 与 smoke 靠这个在不接显示器的环境里验证 Qt 真的可用。

### 9.2 按 import 裁剪（体积关键）

PySide6 的 wheel 会带上整个 Qt，`QtWebEngineCore` 一个约 450 MB。开启：

```toml
[tool.py_upper.optimize]
qt = "imports"
```

规则：

1. 扫描 `app/src` 引用的 `PySide6.Qt*` 模块；
2. 用目标解释器导入并解析**传递闭包**；
3. 删除未用到的 wrapper 模块、Qt 库、插件类别、QML 树、shiboken QML 辅助库、Qt 工具二进制、工具翻译；
4. 插件按**依赖的 Qt 模块**判定：需要额外 Qt 模块的插件（如虚拟键盘输入法插件拉 QtQml/QtQuick、`libqpdf` 拉 QtPdf）会被删除；**平台插件类别永不裁剪**（删了应用起不来）；SVG 图标插件走例外保留。

实测（PySide6 6.11.0，macOS arm64）：**1.2 GB → 154 MB**（PySide6 本体 1.1 GB → 104 MB）；再开 `strip_native = true` 可到 141 MB。

限制：动态加载（`importlib`、QML 内 import）静态看不到，这类应用请显式写模块列表；交叉目标无法执行目标解释器，会保留完整 Qt。

---

## 10. 验证与 CI

两道门：

1. **静态校验**（`--verify`）：文件存在、架构匹配、扩展 ABI 后缀、依赖清单、包布局。可重复执行（幂等）。
2. **真 import smoke**（`--run` 之前自动执行）：
   - 用 bundled Python 导入入口模块 + 所有直接声明的第三方依赖；
   - 用打包好的 launcher 再导入一次（`PY_UPPER_SMOKE=1`）；
   - 最后才真正运行应用。

CI（`.github/workflows/validate.yml`）在**每个原生平台**都跑：

```
单元测试 → 本地端到端构建（第三方 wheel、应用自带 native 库、lock/--locked）
→ doctor → 完整 PBS 构建 → 集成运行（headless，断言 GUI OK）
```

---

## 11. 发布

```bash
python tools/build.py --release --identity "Developer ID Application: ..." --notary-profile <profile>
```

- **macOS**：对嵌套 dylib/so 与应用本体签名，`--notary-profile` 时提交公证并 staple。
- **Windows**：需要 `signtool` 与 `PY_UPPER_SIGN_CERT`。
- **Linux**：交给发行版打包/签名体系。

签名顺序由工具保证：嵌套代码由内向外，launcher 最后（否则 bundle 封签会失效）。

---

## 12. 排错手册

| 症状 | 根因 | 处理 |
|---|---|---|
| `Could not find the Qt platform plugin "cocoa"` / 窗口打不开 | 平台插件被裁剪或缺失 | 升级到 ≥0.17.3；平台插件类别现在永不裁剪。临时可把 `qt` 设为 `"all"` |
| `symbol not found: _PyArg_ValidateKeywordArguments` | launcher 用 `RTLD_LOCAL` 加载 libpython | ≥0.16.15 已改为 `RTLD_GLOBAL` |
| `No module named 'main'` | 扩展后缀不是 CPython 识别的形式 | 应为 `.cpython-310-darwin.so`（macOS）/`.cp310-win_amd64.pyd` |
| `import zlib` 失败 / Cython 字符串压缩报错 | 搜索路径缺 `lib-dynload`（Unix）或 `DLLs`（Windows） | ≥0.17.1 已加入 launcher 搜索路径 |
| `PermissionError: ... python3.10` | runtime 拷贝丢了可执行位 | ≥0.17.1 拷贝时保留执行位 |
| `Errno 1: Operation not permitted` 打包时 | 回放源文件权限/元数据 | ≥0.16.10 只拷内容 + 执行位 |
| `Unresolved native dependencies` | 原生依赖闭包里有找不到的库 | 看清单；可选组件用 `[tool.py_upper.native].exclude`；自带库放 `app/src` 会被自动纳入 |
| `@rpath/xxx.dylib` 被当成依赖 | 把 `LC_ID_DYLIB` 当 `LC_LOAD_DYLIB` | ≥0.16.13 已区分 identity 与依赖 |
| `No PBS runtime metadata for exact Python X` | 元数据缓存被污染或版本不存在 | 删除 `.cache/pbs/uv-download-metadata.json` 重试；确认 PBS 有该精确版本 |
| 构建卡在 native dependency 很久 | 依赖闭包退化成全树扫描 | ≥0.17.1 已改为一次建索引 |
| `codesign` 报 `code object is not signed at all` | 嵌套 bundle 未按由内向外签名 | ≥0.17.2 按路径深度倒序签名 |
| 应用体积 1 GB+ | 完整 Qt 被打包 | 设 `qt = "imports"` |
| pip 下载大 wheel 超时 | 默认 15s socket 超时 | 设 `timeout`/`retries` 或 `PY_UPPER_PIP_TIMEOUT` |
| VS Code 调试报 `No module named 'Cython'` | 探测 Cython 时抛预期异常 | ≥0.16.8 改用 `importlib.metadata`，不会停在异常上 |

调试技巧：`PY_UPPER_TRACEBACK=1 python tools/build.py`；`python tools/build.py --doctor` 看宿主/目标/工具链；`--verify` 可反复执行定位到具体失败项。

---

## 13. 版本与提交约定

- 每个版本一个提交 + 一个 tag（`v0.17.x`），提交信息包含 Problem / Root cause / Changes / Verification / Impact。
- `CHANGELOG.md` 记录面向使用者的变更与限制，`HISTORY.md` 记录简版演进。
- 版本号在 `app/pyproject.toml` 的 `[project].version`；测试从配置读取，不再硬编码。
- `runtimes/`、`build/`、`dist/`、`.cache/` 均不入库。

---

## 14. 模块职责

| 模块 | 职责 |
|---|---|
| `config.py` | 目标契约、配置解析、平台标签、pip 传输参数 |
| `toolchain.py` | 宿主工具链（clang/gcc/MSVC）解析 |
| `pbs_assets.py` / `pbs_sdk.py` / `runtime.py` | PBS 解析、SDK/runtime 获取与校验 |
| `third_party.py` | wheel 解析/安装、site 优化、清单 |
| `qt_prune.py` | 按 import 裁剪 Qt 负载 |
| `python_build.py` | Cython 化、目标扩展编译、staging |
| `native/inspect.py` | 二进制格式与架构识别 |
| `native/deps.py` | 依赖解析（otool/readelf/dumpbin + 索引/缓存） |
| `native/bundle.py` | 依赖闭包、install name 重写、ad-hoc 签名 |
| `package.py` | 产物组装、原生排除 |
| `verify.py` / `smoke.py` | 静态校验与真 import smoke |
| `launcher_build.py` / `launcher/` | C++ launcher 构建与实现 |
| `lock.py` / `manifest.py` / `release_artifacts.py` | 可复现性与发布清单 |
