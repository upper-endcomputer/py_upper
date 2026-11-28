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
2. `runtime.py` / `pbs.py` 获取目标 runtime 和 SDK；`third_party.py` 按目标标签安装 wheel，第三方包不做 Cython 转换。
3. `python_build.py` 编译应用模块并维护增量缓存；`egg.py` / `egg_loader.py` 管理可选归档。`merge=all/directory/none` 是已有配置选择。
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
| runtime / wheel / 裁剪 | `test_pbs.py`、`test_third_party.py`、`test_qt_prune.py`、`test_trim.py` |
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
