# py_upper

`py_upper` 是一个面向 Windows、macOS、Linux 的独立 Python 应用运行时与打包工程。

当前稳定开发线：**0.17.21**。

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
- 应用模块增量编译：只有内容变化的模块重新 Cython 化并编译，其余复用 `build/cache/` 里的产物；多个模块并行编译（`[tool.py_upper.build]`）。
- 第三方依赖不 Cython 化，而是根据 Target 的 wheel compatibility tags 安装目标 wheel。
- `app/src/` 下的 `.dylib/.so/.dll/.pyd` 是正式应用输入，会参加 native dependency closure。
- `runtimes/` 是机器本地 Runtime 缓存，不进入 Git。
- `dist/` 只在一次 rename 中更新：bundle 在 `build/publish/<target>.<pid>/` 组装完成后再原子换入 `dist/`。构建期间 `dist/` 要么是上一版的完整产物，要么是新版的完整产物，不会被原地删改——发布路径上的 runtime 会被编辑器等外部进程执行，原地清理必然与它们的写入互相踩。
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

项目名固定为 `py_upper`。最终 App 名称只在**一处**配置：本地配置 `app/pyproject.toml`（不入库，克隆后由 `app/pyproject.toml.example` 复制而来）的 `[tool.py_upper.app]` 节，当前是 `Upper`：

```toml
[tool.py_upper.app]
# APP NAME, single source of truth: 产物名 / Info.plist / Windows 版本资源
# 都从这一行派生，并被编译进 launcher（PY_UPPER_APP_NAME）。改名后必须重新构建。
name = "Upper"
identifier = "com.example.pyupper"   # macOS bundle id，与名字无关
```

### 改名怎么操作

1. 改上面那一行 `name = "..."`——`app/pyproject.toml`（工作副本）和 `app/pyproject.toml.example`（入库模板）各有一份，两边都改，否则新克隆的人得到的还是旧名；
2. 重新构建：`python tools/build.py --clean`。

**旧产物不会自动删除**：构建只写当前名字的产物，`dist/` 里从 `MyApp` 时代的 `MyApp.app`（或 `MyApp/`）会留着，双击旧的那个跑的还是旧包。`--clean` 是唯一会替你清理的开关，它把 `build/` 和 `dist/` 整个删掉；不想全量重建就手工删 `dist/` 下的旧 bundle。

### `name` 决定什么

- 产物名：macOS 的 `dist/<name>.app`，含 `Contents/MacOS/<name>` 可执行文件与 `Info.plist` 的 `CFBundleExecutable` / `CFBundleName` / `CFBundleDisplayName`；Windows / Linux 的 `dist/<name>/` 目录与 `dist/<name>/<name>(.exe)`；
- Windows 版本资源（`FileDescription` / `ProductName` / `OriginalFilename`）；`Info.plist` 里只有 `CFBundleIdentifier`（来自 `identifier`）和版本字段来自别处，其余名字字段都跟它走；
- 运行时的 `PY_UPPER_APP_NAME`：这个值是**打包时编译进 launcher** 的，所以改名必须重新构建才生效，只改配置文件不会影响已经打好的包；
- 应用内展示位：应用侧统一调 `utils.paths.app_name()` 读它，示例 MVVM 应用直接把它当主窗口标题（`app/src/application.py` → `MainViewModel`）。

### `name` 不影响什么

- **可执行文件允许被改名**：入口程序编译在 launcher 里（见 `docs/DEVELOPMENT_GUIDE.md` §10.1），把 `Upper.app` 改成 `Renamed.app` 或把 `Upper.exe` 改成 `Renamed.exe` 照常启动；
- `identifier` 与项目名 `py_upper`（Python 包名、`tools/` 命令名）都跟这个值无关；
- 开发态（没有 launcher）`app_name()` 会回读同一份 `pyproject.toml`，所以「`PYTHONPATH=src python src/main.py` 时看到的窗口标题」和打包产物自然一致。

### 不要做的事

**不要在应用代码里再写一份名字。** 名字的唯一来源就是 `[tool.py_upper.app].name`。`app/src/utils/paths.py` 的 `_DEFAULT_APP_NAME` 与 `tools/py_upper/config.py` 的 `or "Upper"` 是「既没有 launcher、也读不到 `pyproject.toml`」时的兜底回退值，两处互为镜像，**改名时同样要一起改成新名**。

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

`build/cache/` 保存上一次构建的 Cython 产物与目标扩展，只有变化的模块会重新编译。`[tool.py_upper.build]` 的 `jobs`（默认 `0` = 每 CPU 一个任务）控制并行度，`incremental = false` 强制全量重编译，`--clean` 会连同缓存一起清掉。

## Target Python / PBS

PBS 模式只需要配置一个精确的目标 Python 版本。Windows 目标的发布 runtime 用 python.org 官方 embeddable ZIP，PBS full SDK 只用于编译与依赖解析；其他平台继续用 PBS `install_only_stripped` runtime。精确版本必须同时有对应官方 ZIP 与 PBS SDK（模板用 `3.13.5`；`3.11.13` 没有官方 ZIP）。

```toml
[tool.py_upper.runtime]
provider = "pbs"
python = "3.13.5"
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

可选的 Qt 负载开关（不设置时保持旧行为；`qt = "all"` 时全部不生效）：

```toml
[tool.py_upper.optimize]
qt_software_opengl = false                  # 不打包 opengl32sw.dll（约 20 MB）
qt_locales = ["zh_CN"]                     # 只保留指定语言；"all" 保留整套
qt_image_formats = ["gif", "ico", "jpeg"]  # 不写 "svg" 会同时删除 SVG 图标引擎和 Qt5Svg/Qt6Svg
qt_platforms = ["windows", "cocoa", "xcb", "wayland", "offscreen"]  # 同一份配置覆盖三平台
```

Qt 裁剪还会删除 wheel 里的 `docs`、`scripts`、`support` 和 `*.lib`，以及
shiboken 中只被 C++ 工具链使用的 `vcamp140.dll` / `concrt140.dll` /
`vcomp140.dll`。平台插件按规范化后的名字匹配（`qwindows.dll`、
`libqcocoa.dylib`、`libqxcb.so`、`libqwayland-*.so` 都适用），所以 Windows /
Linux / macOS 不需要各写一套规则。


实测：PySide6 6.11.0 的 macOS arm64 应用从 **1.2 GB 降到 153 MB**（PySide6 本体 1.1 GB → 104 MB），窗口照常打开、`--verify` 通过、`codesign --verify --deep --strict` 通过；再开 `strip_native = true` 可到 141 MB。

限制：动态加载的模块（`importlib`、QML 文件里 import）静态看不到，这类应用请显式列模块；交叉目标无法执行目标解释器，会保留完整 Qt。

## 裁剪接口（`[tool.py_upper.trim]`）

Qt 裁剪只删除 import 能证明的负载。还有一类任何应用都不需要的负载由代码内置的
全局规则直接删除，不用写进配置文件：

- `site-packages`：`**/*.dist-info`、`**/*.egg-info`（pip /
  `importlib.metadata` 的安装元数据）、`**/*.pyi`（类型存根），以及
  `PySide*/`、`PyQt*/` 下的 `examples` 和 `include`；
- `runtime`：`**/idlelib`、`**/test`、`**/tests`、`**/include`、
  `**/pkgconfig`（CPython 的开发工具、测试和 C 头文件）。

这些规则对 Windows / macOS / Linux 一致，先于 preset 和项目配置生效。`keep`
仍然优先：确实要保留 `importlib.metadata` 数据、某个包自带的 tests 或 Tk 时，
用 `site_packages_keep` / `runtime_keep` 把路径保护起来。Tk/Tcl 是否保留取决于
应用，所以不放进全局规则，Qt 应用可以在配置里显式删除：

```toml
[tool.py_upper.trim]
preset = "safe"
site_packages = []
site_packages_keep = []
runtime = ["**/tkinter", "**/tcl[0-9]*", "**/tk[0-9]*",
           "**/itcl[0-9]*", "**/thread[0-9]*"]
runtime_keep = []
```

模式是相对各自根目录的 glob：`*` 不跨目录，`**` 跨目录；命中目录删除整棵子树；
`keep` 优先并保护整个子树。单次构建可用 `--trim-site`、`--trim-site-keep`、
`--trim-runtime`、`--trim-runtime-keep` 重复追加，不修改配置文件。日志会打印
两棵树各自释放的体积。模板默认只保留全局规则，项目自己的删除项写在
`app/pyproject.toml`；`opengl32sw.dll`、`qoffscreen` 等运行时兜底仍由 Qt 裁剪
单独保留。

`preset = "minimal"`（或单次 `--trim-preset minimal`）在全局规则和 safe 之外再删除
`opengl32sw.dll`（约 20 MB）、`qdirect2d` / `qminimal` 平台插件、Qt 的
`translations/*.qm`，以及 `qwebp` / `qtiff` 图片插件。规则不绑定宿主布局：
`PySide*/**/...` 与 `PyQt*/**/...` 同时命中 Windows 平铺的
`PySide2/plugins/...`、macOS/Linux 的 `PySide6/Qt/plugins/...`、PyQt5 的
`PyQt5/Qt5/...` 和 PyQt6 的 `PyQt6/Qt6/...`；`examples`、`include` 也按同一方式
覆盖四种 binding。实测同一 Windows x86_64 PySide2 包从 **92.4 MB / 279 文件**
降到 **64.6 MB / 221 文件**（`site-packages` 77.4 MB → 48.4 MB，runtime
15.3 MB，launcher 0.92 MB）。代价是目标机器必须有可用的 OpenGL 驱动，且应用
不能依赖被删的平台插件、翻译或 WebP/TIFF 解码；VM、RDP 或无 GPU 驱动的机器
可能因此起不来。默认仍是 `safe`，确认目标环境后再切到 `minimal`。四种布局由
单元测试覆盖；完整构建只在 Windows x86_64 上实测，macOS/Linux 需在对应宿主
执行一次 `[1/6]`…`[6/6]`。

`preset = "release"` 仍可识别，但只是兼容别名：`dist-info` / `egg-info` 已经是
全局规则，所以它和 `safe` 没有区别。删除安装元数据后，运行时调用
`importlib.metadata.version("...")` 或 `pkg_resources.require("...")` 会失去数据；
确认应用不依赖它们，或者用 `site_packages_keep` 保住对应分发。

## 加密 egg（默认开启）

`[tool.py_upper.egg]` 把应用自己的 Python 模块和本次构建产出的扩展收进一个
`bundle/py_upper.dat`，不再以散文件留在 `site-packages`。归档格式是
`MAGIC (8 字节) + XOR(zip, SHAKE-256 key stream)`；launcher 在导入应用入口前
安装内存导入器，所以产物里不会再有 `main.py` / `main.pyd`。

加密默认开启：即使项目没有写 `[tool.py_upper.egg]` 段，普通构建也会生成
`py_upper.dat`。要临时发一个散文件版本，用 `--no-egg`；要长期关闭，在配置里
写 `enabled = false`。

```toml
[tool.py_upper.egg]
enabled = true
key_hex = "047636cf3ddbaae2346f597256416e858ee0b7c8b7448221e5babfbb4f74c20d"
backend = "auto"          # auto / memory / extract
compression = "deflated"  # deflated / stored / lzma
```

只有应用自己的代码进 egg：`app/src` 下的 Python 模块、本次构建编译出的
`.pyd`/`.so`，以及随应用分发的其它非原生文件。第三方 wheel、runtime 和
`[tool.py_upper.native]` 依赖闭包需要看到的原生输入仍留在 `site-packages`，
否则闭合检查看不见它们。

后端有三档：

- `memory`：仅 Windows。扩展模块通过 vendored `_memimporter` 直接从内存映射，
  需要 Zig 工具链；编译产物缓存在 `build/cache/memimporter/<target>/`。
- `extract`：所有平台。启动时解密到按内容寻址的缓存目录（目录名是解密后
  归档的 SHA-256）并插入 `sys.path`；首次启动解压一次，之后直接复用，重新
  构建的 egg 会自然切换到新目录。缓存根默认是
  `~/Library/Caches/py_upper/eggs`（macOS）、
  `%LOCALAPPDATA%/py_upper/cache/eggs`（Windows）、
  `$XDG_CACHE_HOME/py_upper/eggs` 或 `~/.cache/py_upper/eggs`（Linux），可用
  `PY_UPPER_EGG_CACHE` 覆盖；目录不可写时回退到 `py_upper_egg_*` 临时目录。
  Linux 没有稳定的内存 dlopen 接口；macOS 开启 hardened runtime 时，缓存目录
  里未签名的 `.so` 可能被 dyld 拒绝，需要
  `com.apple.security.cs.disable-library-validation`，或改用签名后的独立原生库。
- `auto`：Windows 且 `_memimporter` 可用时走 memory，否则 extract。Windows 上
  编译失败只打印 warning 并回退；显式 `memory` 则直接报错，不会静默产出跑不
  起来的包。

单次构建可以覆盖配置，不写回文件：

```bash
python tools/build.py --egg
python tools/build.py --no-egg
python tools/build.py --egg --egg-key <HEX> --egg-backend extract --egg-compression lzma
```

Windows/Linux 下归档位于可执行文件旁的 `bundle/`，macOS 下位于
`Contents/Resources/bundle/`，文件名固定为 `py_upper.dat`，不再暴露 egg/zip
字样。stage 下的 `egg.json` 记录本次构建实际使用的 key/backend/compression，
`--verify` 和冒烟脚本以它为准，不会拿项目默认值去解一个被 CLI 覆盖过的 egg。
`--verify` 会检查 `bundle/py_upper.dat` 的 MAGIC、成员列表，并确认已打包的应用文件不再以
散文件出现；`memory` 后端还必须带 `_memimporter.pyd`。

**加密不等于变小**：SHAKE-256 密钥流只提高直接读源码/反编译的成本，key 本身
编在 launcher 里，不是秘密存储格式；压缩后的 egg 与普通 zip 体积相当。产物
大小仍由 runtime、第三方 wheel 和 Qt 决定，减小体积请用上面的 trim、Qt 裁剪
和 `strip_native`。

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

`launcher/` 是不链接 libpython 的 C++ 可执行文件：运行时从 bundled runtime 解析 CPython 符号，所以换目标 Python 版本不需要重新链接。**入口程序（以及 `PY_UPPER_SMOKE=1` 用的 smoke 程序）在打包时编译进 launcher**，所以产物里没有 `.int` 脚本，**把可执行文件改名后应用照样能起来**，也不会出现"sidecar 被删掉或被改坏"这种故障。手工构建、没有生成源可内嵌的 launcher 才回退到按 `<可执行名>.int` → `.py` → `.pyw` → `_py_upper_static.int` 找入口文件。

launcher 还向应用导出 `PY_UPPER_HOME`（入口目录）、`PY_UPPER_RUNTIME`、`PY_UPPER_SITE_PACKAGES`、`PY_UPPER_EXECUTABLE`，以及仅当入口真是文件时的 `PY_UPPER_SCRIPT`，应用不必靠数父目录猜自己在哪。

致命错误先写 stderr，再按需弹原生对话框（Windows `MessageBoxW`、macOS `CFUserNotification`、Linux `zenity`/`xmessage`）——双击启动没有终端时，这是唯一能看到失败原因的通道。应用抛未捕获异常时，bootstrap 先把 traceback 落到 `TMPDIR/py_upper-<pid>.log`，`Py_FinalizeEx` 之后由 launcher 上报。CI 与自动化设 `PY_UPPER_NO_DIALOG=1`，失败只体现为退出码，不会卡在没人能点的对话框上。

Windows 默认是 GUI 子系统（双击不弹控制台），从 cmd/CI 启动时自动 `AttachConsole` 到父控制台，并且链接静态 CRT（`/MT`），目标机不需要 VC++ 运行库。完整契约与退出码见开发指南 §10。

## 验证

应用入口会打开 Qt 主窗口。CI 与 smoke 通过 `PY_UPPER_HEADLESS=1` 用 offscreen 平台构建同一个窗口后立即退出，因此既证明 Qt 可用，又不会卡住无显示环境。

`--verify` 是静态检查，`--run` 之前的最后一道门是**用目标 Python 真 import**：先由 bundled runtime 导入入口模块和所有直接声明的第三方依赖，再由打包好的 launcher 导入一次。这一步能抓住静态检查看不到的问题（扩展链接方式、stdlib 扩展目录、第三方依赖缺失、Qt 裁剪过度）。

CI 在每个原生平台上都跑同一套真实链路：单元测试 → 本地端到端构建（第三方 wheel、应用自带 native 库、lock/`--locked` 往返）→ doctor → 完整 PBS 构建 → 运行。本地 E2E 默认跳过，设置 `PY_UPPER_E2E=1` 打开。触发范围只有 develop（`push` 与面向 develop 的 `pull_request`）：`feature_*` 分支上的中间提交不跑矩阵，需要单独验证时用 `gh workflow run validate.yml --ref <branch>`。

## Git

Git 历史完整保留，包括之前的 `v0.16.x` 版本；v0.17.0 是一次结构性重构，不改写旧 tag。`runtimes/`、`build/`、`dist/` 和 Python cache 均由 `.gitignore` 忽略。
