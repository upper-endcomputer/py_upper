# appbuild 框架总览

这份文档把构建工具、原生启动器和示例应用放在同一张职责图上。配置细节与命令参数仍以 [开发使用指南](DEVELOPMENT_GUIDE.md) 为准，模板在 `app/pyproject.toml.example`。

## 三个层次与公共入口

```text
应用 pyproject.toml + src/ + 可选 resources/
                    │
                    ▼
tools/build.py → Target Contract → runtime / wheels / Cython / launcher
                    │
                    ▼
staging → 原生依赖闭包 / 裁剪 → 候选包验证 / import smoke → dist
                    │
                    ▼
原生 launcher → bundled CPython → main.py → application.main()
                    │
                    ▼
MVVMQtApplication → MVVMApplication → Model / Service / ViewModel / View
```

| 层次 | 所有者与职责 | 边界 |
| --- | --- | --- |
| 构建与打包 | `tools/build.py` 编排，`tools/appbuild/` 实现 | 读取应用输入；宿主 Python 运行构建工具，Target 决定运行时 ABI 和 wheel |
| 原生启动 | `launcher/src/PyUpper.cpp` | 定位自身与 bundle、装载 libpython、配置隔离解释器、注册 Cython 合并模块、执行内嵌入口 |
| 应用骨架 | `app/src/application.py` 装配对象图 | 示例可替换；业务代码不反向导入构建工具；MVVM 不是打包器的强制依赖 |

配置只从当前应用的 `pyproject.toml` 读取。仓库模板不隐式替代本地配置；`--app` 选择应用目录，`build/` 和 `dist/` 仍属于构建工程。应用名称和安装根在打包运行时由 launcher 导出，源码开发时由 `utils.paths` 回退解析。

## 构建链路的接合点

1. `appdir.py` 在配置导入前选定应用；`config.py` 提供 Target、目标 Python 和配置策略。
2. `runtime.py` / `pbs.py` 获取目标 runtime 和 SDK；`wheels.py` 按目标标签安装 wheel，第三方包不做 Cython 转换。
3. `application_build.py` 编译应用模块并维护增量缓存；`egg.py` / `egg_loader.py` 管理可选归档。`merge=all/directory/none` 是已有配置选择。
4. `package.py` 编译 launcher 并组装目录；`native/` 解析与补齐动态库闭包；`trim.py` / `qt_prune.py` 实施保留与裁剪策略。
5. Linux 本机构建在发布前预编译标准库 checked-hash 字节码。launcher 禁止在安装目录写字节码；跨架构构建不运行目标解释器。
6. `verify.py` 检查布局、架构和依赖，再执行目标 Python 与 launcher import smoke。`--run` 继续调用实际应用入口。

发布替换采用“旧包暂存 → 新包 rename → 删除旧包”，新包 rename 失败恢复旧包。两次 rename 有间隙，进程被强制终止的恢复不在当前保证内；CLI 先在候选目录完成静态与 smoke 检查，非 Linux 正式发布还先完成签名与签名后 smoke；失败不会替换旧应用。

## 应用状态与资源归属

| 对象 | 创建位置 | 释放责任 |
| --- | --- | --- |
| QApplication | `create_application()`，复用已有实例 | Qt 适配器管理事件循环；`quit()` 请求退出 |
| 根 Model、Service | `application.py` / 容器初始化 | 示例为普通 Python 对象；目前没有统一 Service shutdown 协议 |
| ViewModel | `initialize_viewmodels()` | 注册后由 `MVVMApplication.stop()` 释放，每个实例按一个名字注册 |
| View | `initialize_views()` | 容器调用 `close()` 后 `dispose()` |
| 属性绑定、Qt 命令信号 | View 的绑定方法 | `ViewBase.dispose()` 逐项断开 |
| View 自建订阅 | 具体 View，例如 MainWindow | 具体 View 保存 unsubscribe，并在 `dispose()` 调用后再交给基类 |

一个 ViewModel 可以供多个 View 共享。销毁某个 View 不能清空其他 View 依赖的 ViewModel。未注册进容器的 ViewModel 由创建方负责释放。

`MVVMQtApplication.run()` 是生命周期入口：

```text
prepare()：建容器 → 注册 ViewModel → 注册 View → start()
                  （容器正在运行时直接复用）
事件处理：交互模式 exec_loop() / headless 模式 processEvents()
finally：stop() → close/dispose 所有 View → dispose ViewModel → 清空注册表
```

正常返回、初始化中断、事件处理异常都经过同一清理出口。`quit()` 可主动结束运行，随后再次 stop 不重复清理已清空的注册表。具体资源的清理方法应可重复调用；清理会逐项尝试所有 close/dispose，清空注册表后汇总报告错误；某个对象失败不再跳过其他对象。

扩展示例时把业务状态放入 Model/ViewModel、能力通过 Service 注入、控件操作留在 View。Qt 类型通过 `utils.qt_compat` 访问；在实际创建控件时导入，保持入口导入无需 Qt。About 控件仍在首次显示时创建，原生关闭通过 `finished` 信号接到 `hide_about` 命令，保持可见状态同步并允许重新打开。

## 验证入口与覆盖

从仓库根运行；依赖、工具链和本地配置准备见开发指南。

```bash
python tools/build.py --doctor --target linux-arm64
python -m pytest app/tests -q
APPBUILD_E2E=1 python -m pytest app/tests -q
APPBUILD_HEADLESS=1 APPBUILD_NO_DIALOG=1 python tools/build.py --target linux-arm64 --run
python tools/build.py --target linux-arm64 --verify
```

目标应换成实际宿主目标；`--run` 不支持跨架构运行。正常桌面运行去掉 `APPBUILD_HEADLESS=1`。

| 接合点 | 主要回归文件（统一位于 `app/tests/`） |
| --- | --- |
| 配置、目标、宿主兼容 | `test_config.py`、`test_build_tool_compat.py`、`test_doctor.py` |
| runtime / wheel / 裁剪 | `test_pbs.py`、`test_wheels.py`、`test_qt_prune.py`、`test_trim.py` |
| Cython / egg / 增量缓存 | `test_incremental_build.py`、`test_merged_build.py`、`test_egg.py` |
| 打包 / ABI / 原生闭包 | `test_package.py`、`test_verify.py`、`test_native_deployment.py`、`test_manifest.py` |
| 启动 / 安装迁移 | `test_launcher_deployment.py`、`test_e2e_local.py`、`test_startup_bytecode.py` |
| MVVM / Qt 适配 / 生命周期 | `test_mvvm.py`、`test_mvvm_example.py`、`test_qt_compat.py` |
| 前轮修复回归 | `test_audit_regressions.py`（通知、订阅、发布回滚、安全解包、重试） |

`app/tests` 统一承载构建工具、应用与接合点测试；替换示例 app 时应保留构建器回归文件。fake Qt 用例验证对象图与生命周期；真实 Qt 构建冒烟验证动态库与插件可加载。offscreen 成功不能替代真实桌面首帧、硬件通信或其他操作系统实测。

## 已有能力与待完善边界

- CAN、Serial、OTA 当前只有数据模型，未实现设备通信、线程调度和真实升级流程。
- 异步业务可选择 `run_async()` 与 AsyncLifecycle，显式登记任务和服务关闭；同步入口不引入 qasync 依赖。详见 [发布与异步说明](RELEASE_AND_ASYNC.md)。
- `PySide6-Essentials` 和 `merge=directory` 是按应用需求选择的优化，不强制写入通用模板；前者缩小构建输入，后者改变增量编译粒度，均不能直接等同于启动加速。
- Linux `--release` 已接入 CPack，默认 TGZ/DEB、可选择 RPM，独立目标目录发布。旧根级归档不会随 `--run` 更新；依赖扫描及实机安装限制见 [发布与异步说明](RELEASE_AND_ASYNC.md)。
- Windows/macOS 与真实 Linux 桌面按 CI/实机矩阵验证；当前容器运行结果仅代表此环境。

## 脚本与遗留接口收口

正式维护范围以 Git 跟踪文件为准，`.tmp/`、`build/`、缓存与 runtime 内的开发脚本不属于应用入口。当前 Python 文件共 88 个：应用 24、测试 30、构建及维护代码 29、内置 Tomli 5。模块文件不能等同于可执行脚本。

| 收口项 | 调用证据与结果 |
| --- | --- |
| `mvvm/core/factory.py` | `ViewModelFactory` 仅被导出和结构测试引用；应用直接装配 ViewModel。`PropertyFactory` 的四次调用重复了 `_register_property` 的赋名行为。删除工厂模块，直接构造 `ObservableProperty`。 |
| `appbuild/build_info.py` | 唯一功能是输出构建来源清单，移入 `manifest.write_build_info`，CLI 与回归同步更新。 |
| `appbuild/workspace_lock.py` | 唯一功能是工作区文件锁，移入 `fs.workspace_lock`，保留锁冲突、释放和帮助入口回归。 |
| `third_party.installed_import_names` | 全仓无调用；实际依赖冒烟使用 `_direct_imports_from_requirements`，删除旧扫描入口。 |

保留 `appdir` 的配置导入前路径选择边界；`compat` 和 Tomli 用于 Python 3.8–3.10，属于当前支持能力；独立 egg loader 要被嵌入启动器，不能直接依赖构建器模块。桌面探针、基准复现和中文说明检查仍各有文档或 CI 入口。旧模块路径和工厂接口不提供转发兼容层。

本轮收口验证：全量回归 498 passed / 6 skipped；显式真实 E2E 3 passed；Linux ARM64 正式 `tools/build.py --run` 输出 `GUI OK offscreen` 并以 0 退出；129 个跟踪文件维护检查通过。原工厂结构测试替换为属性赋名、通知和释放的行为回归。

### appbuild 模块命名与发布边界

- `input_lock.py` 固定构建输入；互斥写锁位于 `fs.workspace_lock`。
- `wheels.py` 负责 wheel 解析、安装和 site 优化，替代过宽的 `third_party.py` 名称。
- `application_build.py` 编译并暂存应用负载，替代容易与 Python runtime 构建混淆的 `python_build.py`。
- `release.py` 合并原 `signing.py` 和 `linux_release.py`：`sign_macos_files` 处理归档前扩展签名，`sign_release` 处理最终候选包签名及公证，`build_linux_installers` 生成 Linux 安装包。安装包函数内部才导入 package 发布辅助，避免底层签名加载完整组装依赖链。
- `runtime`、`pbs`、`package` 分别保留运行时装配、供应源获取、候选包组装的职责边界。模块迁移不改变 CLI 参数、配置段、锁文件名或产物格式，不提供旧模块转发别名。

本轮命名与发布合并验证：498 passed / 6 skipped，真实 E2E 3 passed；正式 CLI `--release --linux-format TGZ` 生成 Linux ARM64 安装归档，归档包含应用与启动链接；打包应用输出 `GUI OK offscreen`，退出码 0。当前 appbuild 顶层实现模块共 19 个（不计包入口）。
