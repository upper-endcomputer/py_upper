# py_upper

`py_upper` 是一个面向 Windows、macOS、Linux 的独立 Python 应用运行时与打包工程。

当前开发线：**0.16.1**。

## 核心原则

- `app/src/` 直接就是应用源码，不再套 `myapp/` 之类的项目包名。
- 开发 Python 与最终随 App 打包的 Target Python 完全独立。
- Cython 只处理明确选择的业务模块；入口 `main.py` 保持普通 Python。
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

## Python 环境

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

- Windows: x86 / x86_64 / arm64
- macOS: x86_64 / arm64
- Linux: x86_64 / arm64

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

此前的 PyStand2 历史版本完整保留在 Git tag 中；本次重构从 `v0.16.0` 延续到 `v0.16.1`，不会改写旧版本的历史内容。
