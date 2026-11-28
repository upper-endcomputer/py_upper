# py_upper

面向 Windows、macOS、Linux 的独立 Python 应用构建与交付工具。成品自带 CPython，用户无需安装 Python。当前开发线 **0.17.37**。

构建链路：应用源码与配置 → 目标 runtime/SDK/wheels → Cython 与 native 编译 → 候选包组装与裁剪 → 静态检查、目标 import、launcher smoke → 发布到 `dist/`。

## 快速开始

构建工具需要 Python 3.8–3.13，以及目标平台的 C/C++ 编译工具；安全解包需要带 `tarfile.data_filter` 更新的 Python。先复制配置模板：

```bash
python -c "import shutil; shutil.copyfile('app/pyproject.toml.example', 'app/pyproject.toml')"
python tools/build.py --doctor
```

模板的 `[project].dependencies` 为空。运行仓库示例 GUI 时，先在本地配置中声明适合目标 Python 和平台的 `PySide6`；串口、BLE、异步入口按实际使用声明 `pyserial`、`bleak`、`qasync`。自有应用只声明自己的依赖。

```bash
python tools/build.py
python tools/build.py --run
# 接入自己的应用：目录含 pyproject.toml、src/ 与可选资源
python tools/build.py --app /path/to/app
```

构建器安装目标 wheel；源码开发环境的依赖需另行安装。改应用名或需要清理重建时，依次执行 `python tools/build.py --clean` 和 `python tools/build.py`；clean 本身不构建。

## 输出与平台

成品位于 `dist/`：Windows/Linux 是应用目录，macOS 是 `.app`。分发整个成品，不单独复制 launcher。应用配置决定产品名、版本、依赖和入口；构建器提交与配置摘要另记在 `build-info.json`。

| 平台 | 构建目标 | 当前 CI GUI 检查 |
| --- | --- | --- |
| Windows | x86、x64、ARM64 | x64/ARM64 headless；x86 无 GUI run |
| macOS | Intel、ARM64 | headless readiness |
| Linux | x86_64、ARM64 | x86_64 headless；ARM64 无 GUI run |

CI 的构建与 headless 检查不代替正式签名、公证、真实桌面或安装升级验收。应用源码默认 Cython 化；第三方 wheel 保持原形。egg 增加直接读取成本，不承担密钥保管或授权安全职责。

## 开发与验证

```bash
python -m pytest -q
# 需要本机编译器，显式启用真实构建 E2E
# Linux/macOS:
PY_UPPER_E2E=1 python -m pytest tests/tools/test_e2e_local.py -q
```

工具回归位于 `tests/tools/`，示例回归位于 `app/tests/`。同一工作区构建使用互斥锁；多应用并行需要独立工作区。候选包通过检查后才替换旧包，发布不保证断电事务恢复。

## 文档入口

- [开发指南](docs/DEVELOPMENT_GUIDE.md)：环境准备、开发与排错。
- [配置与构建参考](docs/CONFIGURATION_REFERENCE.md)：应用名、目标 Python、第三方依赖、native 闭包、Qt 裁剪、egg、部署和详细配置。
- [架构](docs/ARCHITECTURE.md)：工具与示例的分层边界。
- [发布与异步生命周期](docs/RELEASE_AND_ASYNC.md)：Linux 安装包、签名与桌面验收。
- [优化评估](docs/OPTIMIZATION_REVIEW.md)与[实施记录](docs/OPTIMIZATION_IMPLEMENTATION.md)：问题证据、完成情况和验证边界。
- [收尾实测](docs/OPTIMIZATION_CLOSEOUT.md)：合并模式、Qt 输入、Linux 发布和未关闭验收项。
- [变更记录](CHANGELOG.md)与[历史记录](HISTORY.md)。
