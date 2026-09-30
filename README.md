# py_upper

`py_upper` 是一个面向 Windows、macOS、Linux 的独立 Python 应用运行时与打包工程。

当前稳定开发线：**0.17.0**。

## v0.17.0 的构建模型

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

离线/内网源可以：

```toml
[tool.py_upper.dependencies]
find_links = ["wheelhouse"]
no_index = true
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

## Git

Git 历史完整保留，包括之前的 `v0.16.x` 版本；v0.17.0 是一次结构性重构，不改写旧 tag。`runtimes/`、`build/`、`dist/` 和 Python cache 均由 `.gitignore` 忽略。
