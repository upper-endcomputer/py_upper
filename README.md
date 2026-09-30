# py_upper

`py_upper` 是一个面向 Windows、macOS、Linux 的独立 Python 应用运行时与打包工程。

当前开发线：**0.16.18**。

## 核心原则

- `app/src/` 直接就是应用源码，不再套 `myapp/` 之类的项目包名。
- 开发 Python 与最终随 App 打包的 Target Python 完全独立。
- Cython 默认处理 `app/src/` 下所有非 `__init__.py` 的应用模块；也可通过 `[tool.py_upper.cython]` 的 include/exclude 精确选择。
- Launcher、Runtime、SDK、第三方 wheels 与应用源码分离。
- 最终 App 名称独立于项目名 `py_upper`，可通过配置随时修改。

## 项目结构

```text
py_upper/
├── app/
│   ├── pyproject.toml
│   ├── src/
│   │   ├── main.py
│   │   ├── core/
│   │   ├── services/
│   │   ├── models/
│   │   ├── utils/
│   │   └── plugins/
│   ├── tests/
│   └── resources/
├── launcher/
├── runtimes/
├── tools/
│   ├── build.py
│   └── py_upper/
├── .vscode/
├── build/
└── dist/
```

## VS Code 开发

打开仓库根目录后选择自己的 Python Interpreter。

直接按 **F5** 运行：

```text
app/src/main.py
```

等价命令：

```bash
cd app
python src/main.py
```

不需要安装 `myapp`，也不需要为了运行源码额外设置 package 名称。

## App 名称

项目名固定为 `py_upper`，最终 App 名称独立配置：

```toml
[tool.py_upper.app]
name = "MyApp"
identifier = "com.example.pyupper"
```

例如改成：

```toml
[tool.py_upper.app]
name = "DemoTool"
identifier = "com.example.demotool"
```

之后输出会自动变成：

```text
Windows: dist/DemoTool/DemoTool.exe
Linux:  dist/DemoTool/DemoTool
macOS:   dist/DemoTool.app/Contents/MacOS/DemoTool
```

Launcher 会根据自身 executable 名称自动寻找对应的 `.int` 入口文件，因此不需要重新修改 C++ 源码。

## 构建

唯一公共构建入口：

```bash
python tools/build.py
```

常用选项：

```bash
python tools/build.py --target linux-x86_64
python tools/build.py --run
python tools/build.py --verify
python tools/build.py --lock --target linux-x86_64
python tools/build.py --locked --target linux-x86_64
python tools/build.py --doctor
python tools/build.py --clean
python tools/build.py --release
```

## Target Python / PBS

默认只需要指定你想打包的精确 Target Python 版本：

```toml
[tool.py_upper.runtime]
provider = "pbs"
python = "3.11.10"
```

`[tool.py_upper.pbs].release` 是可选的。省略它时，py_upper 会从 PBS release 列表中自动选择**最新且同时提供该精确 Python 版本、目标平台 runtime 和 full SDK** 的 release，不会把 `3.11.10` 自动替换成其他 patch 版本。

如果需要完全固定 PBS release，可以显式指定：

```toml
[tool.py_upper.pbs]
release = "20241016"
```

锁定构建时，实际解析到的 PBS release 会记录到 lock 文件，因此日常项目配置不需要同时维护 Python 版本和 PBS release 两个版本号。

## Python 环境

### Build Tool Python 兼容性

`tools/build.py` 及 `tools/py_upper/` 的最低开发 Python 版本为 **3.8**。它与最终打包的 Target Python 完全独立。

- Python 3.8–3.10：使用项目内置的兼容 TOML 解析器。
- Python 3.11+：优先使用标准库 `tomllib`。
- 因此使用 Python 3.10 开发时，不需要把开发环境升级到 3.11。
- Target Runtime 仍由 `[tool.py_upper.runtime]` 单独决定。

兼容性验证命令：

```bash
python tools/build.py --doctor
python -m pytest app/tests -q
```

开发 Python 选择顺序：

1. `PYSTAND_PYTHON`
2. `app/.venv`
3. 运行 `tools/build.py` 的 Python
4. `python` / `python3` PATH

因此你的开发机可以使用 Python 3.11，也可以使用其他符合项目要求的版本；它不要求与最终 bundled Python 相同。

Target Runtime 独立配置：

```toml
[tool.py_upper.runtime]
provider = "pbs"
python = "3.13"
```

自定义/旧版本 Runtime：

```toml
[tool.py_upper.runtime]
provider = "local"
python = "3.8.10"
runtime = "runtimes/{target}/{python}"
sdk = "runtimes/{target}/{python}-sdk"
```

Windows XP 场景必须使用真正兼容 XP 的 custom CPython 构建；官方 CPython 3.8.10 本身并不是 XP runtime。

## Target

> `runtimes/` is a local PBS/custom-runtime cache and is intentionally Git-ignored. Runtime binaries are machine/target artifacts; the project configuration and lock/manifest metadata record the runtime contract without committing those binaries.


- Windows: x86 / x86_64 / arm64
- macOS: x86_64 / arm64
- Linux: x86_64 / arm64

## 应用自带 native 库

`app/src/` 下的 native 文件会随应用源码一起进入 staging，并自动进入 native 依赖闭包。macOS 下的 `.dylib`、Linux 下的 `.so`、Windows 下的 `.dll`/native `.exe` 都不需要另外执行手工复制。

例如：

```text
app/src/
├── main.py
├── core/
│   └── app.py
└── native/
    ├── libdevice.dylib
    ├── libhelper.dylib
    └── plugins/
        └── libdriver.dylib
```

打包时 py_upper 会：

```text
src native files
      ↓
site-packages / native
      ↓
架构检查
      ↓
扫描每个 dylib/so/dll 的依赖
      ↓
递归查找同一 App 内及 runtime 中的依赖
      ↓
复制缺失的外部 native 依赖
      ↓
macOS 重写为 @loader_path 相对路径
```

因此不要求 native 库必须来自 PBS。应用自己的预编译 `.dylib` 也可以直接放进 `src`。macOS 官方文档建议嵌入 bundle 的动态库使用相对路径；`install_name_tool -id`/`-change` 可用于将原来的绝对路径和依赖改成 bundle-relative 形式。 citeturn766061search1turn766061search0

需要注意：如果 Cython 扩展是在链接阶段直接依赖自定义 dylib，而不是通过 `ctypes`/插件机制运行时加载，那么扩展的链接参数仍然必须让 target compiler 找到该库；py_upper 负责的是**发布阶段的 native 依赖闭包与重定位**，不会凭空推断 C/C++ 链接时应该链接哪个库。

## 构建流程

```text
Runtime + SDK
    ↓
manifest
    ↓
lock
    ↓
target wheels
    ↓
Cython → C
    ↓
target compiler
    ↓
native extensions
    ↓
PyUpper launcher + runtime
    ↓
package
    ↓
verify
```

## CI

CI 使用原生 runner 验证 Linux x86_64、Linux ARM64、Windows x86_64、Windows ARM64、macOS Intel、macOS ARM64，并上传每个平台的构建产物。

## Git 历史

此前的 PyStand2 历史版本完整保留在 Git tag 中；本次重构从 `v0.16.0` 延续到当前版本，不会改写旧版本的历史内容。
