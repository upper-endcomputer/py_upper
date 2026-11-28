# 逐文件中文维护索引

范围为 Git 跟踪文件；严格 JSON、许可证和类型标记在此记录，代码文件另外有中文维护说明。此表记录文件职责，不代表逐函数审计已经完成。

| 文件 | 职责与维护约束 | 说明位置 |
| --- | --- | --- |
| [.github/workflows/validate.yml](../.github/workflows/validate.yml) | 验证 develop 的宿主兼容与原生目标；headless 检查不代替签名公证和真实桌面验收。 | 文件内中文说明 |
| [.gitignore](../.gitignore) | 排除本机配置、缓存和产物；新增规则时确认不会隐藏应提交的源码。 | 文件内中文说明 |
| [.vscode/extensions.json](../.vscode/extensions.json) | 机器读取的 .vscode/extensions.json；保持合法 JSON，中文说明集中在本索引，避免插入非法注释。 | 索引说明；保持格式或许可 |
| [.vscode/launch.json](../.vscode/launch.json) | 机器读取的 .vscode/launch.json；保持合法 JSON，中文说明集中在本索引，避免插入非法注释。 | 索引说明；保持格式或许可 |
| [.vscode/settings.json](../.vscode/settings.json) | 机器读取的 .vscode/settings.json；保持合法 JSON，中文说明集中在本索引，避免插入非法注释。 | 索引说明；保持格式或许可 |
| [.vscode/tasks.json](../.vscode/tasks.json) | 机器读取的 .vscode/tasks.json；保持合法 JSON，中文说明集中在本索引，避免插入非法注释。 | 索引说明；保持格式或许可 |
| [CHANGELOG.md](../CHANGELOG.md) | 中文项目文档：CHANGELOG.md；历史评估和测试数字保留时间边界，不冒充当前结果。 | 索引说明；保持格式或许可 |
| [HISTORY.md](../HISTORY.md) | 中文项目文档：HISTORY.md；历史评估和测试数字保留时间边界，不冒充当前结果。 | 索引说明；保持格式或许可 |
| [README.md](../README.md) | 中文项目文档：README.md；历史评估和测试数字保留时间边界，不冒充当前结果。 | 索引说明；保持格式或许可 |
| [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) | 中文项目文档：THIRD_PARTY_NOTICES.md；历史评估和测试数字保留时间边界，不冒充当前结果。 | 索引说明；保持格式或许可 |
| [app/pyproject.toml.example](../app/pyproject.toml.example) | 应用配置模板；复制到本地配置后修改产品信息与依赖，不直接硬编码个人环境。 | 文件内中文说明 |
| [app/src/application.py](../app/src/application.py) | 组装示例的 Model、Service、ViewModel 与 View；应用入口负责创建和释放对象图。 | 文件内中文说明 |
| [app/src/main.py](../app/src/main.py) | 启动示例应用；业务装配位于 application，避免入口复制生命周期逻辑。 | 文件内中文说明 |
| [app/src/mvvm/__init__.py](../app/src/mvvm/__init__.py) | 定义 app/src/mvvm 包的导出边界；保留导出对象身份，避免在包初始化时引入额外运行副作用。 | 文件内中文说明 |
| [app/src/mvvm/async_lifecycle.py](../app/src/mvvm/async_lifecycle.py) | 管理应用拥有的异步任务与关闭回调；并发关闭共享任务，等待者取消不应中断清理。 | 文件内中文说明 |
| [app/src/mvvm/core/__init__.py](../app/src/mvvm/core/__init__.py) | 定义 app/src/mvvm/core 包的导出边界；保留导出对象身份，避免在包初始化时引入额外运行副作用。 | 文件内中文说明 |
| [app/src/mvvm/core/base_view_model.py](../app/src/mvvm/core/base_view_model.py) | 提供 ViewModel 属性通知与资源释放基类；界面绑定只依赖通知协议。 | 文件内中文说明 |
| [app/src/mvvm/core/command.py](../app/src/mvvm/core/command.py) | 封装同步与异步命令及可执行状态；异步任务的取消与异常需由调用方观察。 | 文件内中文说明 |
| [app/src/mvvm/core/factory.py](../app/src/mvvm/core/factory.py) | 集中创建可观察属性与 ViewModel；重导出必须保持同一类对象身份。 | 文件内中文说明 |
| [app/src/mvvm/core/observable.py](../app/src/mvvm/core/observable.py) | 维护可观察属性及订阅回调；解除订阅是视图销毁时的必要步骤。 | 文件内中文说明 |
| [app/src/mvvm/models/__init__.py](../app/src/mvvm/models/__init__.py) | 定义 app/src/mvvm/models 包的导出边界；保留导出对象身份，避免在包初始化时引入额外运行副作用。 | 文件内中文说明 |
| [app/src/mvvm/models/about_model.py](../app/src/mvvm/models/about_model.py) | 保存关于窗口的展示数据；数据模型不依赖 Qt 窗口。 | 文件内中文说明 |
| [app/src/mvvm/models/base_model.py](../app/src/mvvm/models/base_model.py) | 提供示例业务模型；CAN、串口与 OTA 数据模型不能视为真实硬件通信实现。 | 文件内中文说明 |
| [app/src/mvvm/mvvm_application.py](../app/src/mvvm/mvvm_application.py) | 保存模型、视图和服务的注册关系；停止时逐项尝试清理并汇总失败。 | 文件内中文说明 |
| [app/src/mvvm/mvvm_bootstrap.py](../app/src/mvvm/mvvm_bootstrap.py) | 通过统一 Qt 适配层创建应用并运行事件循环；复用现存实例时保持对象归属。 | 文件内中文说明 |
| [app/src/mvvm/services/__init__.py](../app/src/mvvm/services/__init__.py) | 定义 app/src/mvvm/services 包的导出边界；保留导出对象身份，避免在包初始化时引入额外运行副作用。 | 文件内中文说明 |
| [app/src/mvvm/services/greeting.py](../app/src/mvvm/services/greeting.py) | 提供可注入的问候服务；业务服务不持有窗口或 ViewModel。 | 文件内中文说明 |
| [app/src/mvvm/view_models/__init__.py](../app/src/mvvm/view_models/__init__.py) | 定义 app/src/mvvm/view_models 包的导出边界；保留导出对象身份，避免在包初始化时引入额外运行副作用。 | 文件内中文说明 |
| [app/src/mvvm/view_models/main_vm.py](../app/src/mvvm/view_models/main_vm.py) | 把模型与服务转换为界面状态和命令；保持无 Qt 导入，便于独立测试。 | 文件内中文说明 |
| [app/src/mvvm/views/__init__.py](../app/src/mvvm/views/__init__.py) | 定义 app/src/mvvm/views 包的导出边界；保留导出对象身份，避免在包初始化时引入额外运行副作用。 | 文件内中文说明 |
| [app/src/mvvm/views/about_window.py](../app/src/mvvm/views/about_window.py) | 构造关于窗口并绑定状态；窗口关闭与释放时解除对应订阅。 | 文件内中文说明 |
| [app/src/mvvm/views/base_view.py](../app/src/mvvm/views/base_view.py) | 实现属性和命令绑定；双向绑定必须提供明确的读回路径。 | 文件内中文说明 |
| [app/src/mvvm/views/main_window.py](../app/src/mvvm/views/main_window.py) | 构造示例主窗口与按钮；业务动作通过 ViewModel 命令执行。 | 文件内中文说明 |
| [app/src/utils/__init__.py](../app/src/utils/__init__.py) | 定义 app/src/utils 包的导出边界；保留导出对象身份，避免在包初始化时引入额外运行副作用。 | 文件内中文说明 |
| [app/src/utils/paths.py](../app/src/utils/paths.py) | 区分源码开发与打包运行的资源路径；安装目录可能只读，不用于保存用户数据。 | 文件内中文说明 |
| [app/src/utils/qt_compat.py](../app/src/utils/qt_compat.py) | 延迟选择可用 Qt binding 并统一 Signal、Slot 与事件循环入口；不在导入适配模块时加载全部 Qt。 | 文件内中文说明 |
| [app/tests/test_app.py](../app/tests/test_app.py) | 验证示例应用入口；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_async_lifecycle.py](../app/tests/test_async_lifecycle.py) | 验证异步任务取消与资源关闭；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_audit_regressions.py](../app/tests/test_audit_regressions.py) | 验证既有审查问题的回归边界；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_mvvm.py](../app/tests/test_mvvm.py) | 验证属性、命令和容器的生命周期；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_mvvm_example.py](../app/tests/test_mvvm_example.py) | 验证Model 到窗口的装配与事件绑定；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_optimization_fixes.py](../app/tests/test_optimization_fixes.py) | 验证缓存、签名、发布和关闭流程的整改边界；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_qt_compat.py](../app/tests/test_qt_compat.py) | 验证Qt binding 延迟导入和统一接口；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/conftest.py](../app/tests/conftest.py) | 为测试提供统一导入路径和假 Qt fixture；假对象验证装配，不能替代真实平台运行。 | 文件内中文说明 |
| [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) | 中文项目文档：docs/ARCHITECTURE.md；历史评估和测试数字保留时间边界，不冒充当前结果。 | 索引说明；保持格式或许可 |
| [docs/CONFIGURATION_REFERENCE.md](../docs/CONFIGURATION_REFERENCE.md) | 中文项目文档：docs/CONFIGURATION_REFERENCE.md；历史评估和测试数字保留时间边界，不冒充当前结果。 | 索引说明；保持格式或许可 |
| [docs/DEVELOPMENT_GUIDE.md](../docs/DEVELOPMENT_GUIDE.md) | 中文项目文档：docs/DEVELOPMENT_GUIDE.md；历史评估和测试数字保留时间边界，不冒充当前结果。 | 索引说明；保持格式或许可 |
| [docs/OPTIMIZATION_CLOSEOUT.md](../docs/OPTIMIZATION_CLOSEOUT.md) | 中文项目文档：docs/OPTIMIZATION_CLOSEOUT.md；历史评估和测试数字保留时间边界，不冒充当前结果。 | 索引说明；保持格式或许可 |
| [docs/OPTIMIZATION_IMPLEMENTATION.md](../docs/OPTIMIZATION_IMPLEMENTATION.md) | 中文项目文档：docs/OPTIMIZATION_IMPLEMENTATION.md；历史评估和测试数字保留时间边界，不冒充当前结果。 | 索引说明；保持格式或许可 |
| [docs/OPTIMIZATION_REVIEW.md](../docs/OPTIMIZATION_REVIEW.md) | 中文项目文档：docs/OPTIMIZATION_REVIEW.md；历史评估和测试数字保留时间边界，不冒充当前结果。 | 索引说明；保持格式或许可 |
| [docs/RELEASE_AND_ASYNC.md](../docs/RELEASE_AND_ASYNC.md) | 中文项目文档：docs/RELEASE_AND_ASYNC.md；历史评估和测试数字保留时间边界，不冒充当前结果。 | 索引说明；保持格式或许可 |
| [docs/benchmarks/2026-11-28-linux-arm64.json](../docs/benchmarks/2026-11-28-linux-arm64.json) | 机器读取的 docs/benchmarks/2026-11-28-linux-arm64.json；保持合法 JSON，中文说明集中在本索引，避免插入非法注释。 | 索引说明；保持格式或许可 |
| [docs/benchmarks/reproduce.py](../docs/benchmarks/reproduce.py) | 复现合并模式构建与 headless 启动采样；会生成 UpperCloseout 产物，运行时需独占工作区。 | 文件内中文说明 |
| [launcher/CMakeLists.txt](../launcher/CMakeLists.txt) | 配置 launcher 编译、平台库与嵌入源码；Windows 运行库和子系统必须与发布契约一致。 | 文件内中文说明 |
| [launcher/CMakePresets.json](../launcher/CMakePresets.json) | 机器读取的 launcher/CMakePresets.json；保持合法 JSON，中文说明集中在本索引，避免插入非法注释。 | 索引说明；保持格式或许可 |
| [launcher/src/PyUpper.cpp](../launcher/src/PyUpper.cpp) | 定位运行时、认证应用归档、注册合并模块并启动 CPython；解释器初始化前的失败需走原生诊断。 | 文件内中文说明 |
| [launcher/src/PyUpper.h](../launcher/src/PyUpper.h) | 声明 launcher 统一入口；参数采用 UTF-8，返回值传递应用或启动错误状态。 | 文件内中文说明 |
| [launcher/src/egg_bundles.h](../launcher/src/egg_bundles.h) | 在 CPython 初始化前认证并提取合并 bundle；校验成员范围和归档结构后才交给加载器。 | 文件内中文说明 |
| [launcher/src/main.cpp](../launcher/src/main.cpp) | 适配操作系统进程入口；Windows 宽字符参数转换后再进入公共 launcher。 | 文件内中文说明 |
| [launcher/src/sha3.h](../launcher/src/sha3.h) | 实现与 Python SHAKE-256 对齐的 Keccak 运算；当前目标为小端平台，修改时必须对照协议测试向量。 | 文件内中文说明 |
| [pytest.ini](../pytest.ini) | 统一收集构建器与示例测试；需要真实构建的 E2E 由环境变量显式启用。 | 文件内中文说明 |
| [app/tests/test_build_tool_compat.py](../app/tests/test_build_tool_compat.py) | 验证宿主版本、平台工具链和 CLI 配置契约；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_cache_leases.py](../app/tests/test_cache_leases.py) | 验证真实 C++ 缓存租约和淘汰行为；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_cython_dependencies.py](../app/tests/test_cython_dependencies.py) | 验证外部声明变更与增量输出一致性；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_doctor.py](../app/tests/test_doctor.py) | 验证构建环境诊断；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_e2e_local.py](../app/tests/test_e2e_local.py) | 验证本机 runtime 与 wheel 的完整打包及迁移启动；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_egg.py](../app/tests/test_egg.py) | 验证归档认证、读取和缓存安全边界；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_incremental_build.py](../app/tests/test_incremental_build.py) | 验证Cython 与原生编译的增量命中及失效；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_launcher_deployment.py](../app/tests/test_launcher_deployment.py) | 验证launcher 部署和路径定位；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_linux_release.py](../app/tests/test_linux_release.py) | 验证Linux 安装包生成及发布边界；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_memimporter_lifetime.py](../app/tests/test_memimporter_lifetime.py) | 编译原生探针验证内存模块释放顺序、引用计数及序号导出边界。 | 文件内中文说明 |
| [app/tests/test_manifest.py](../app/tests/test_manifest.py) | 验证清单摘要和文件覆盖；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_merged_build.py](../app/tests/test_merged_build.py) | 验证多模块合并及注册符号；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_native_deployment.py](../app/tests/test_native_deployment.py) | 验证原生库闭包、架构和重定位；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_package.py](../app/tests/test_package.py) | 验证候选目录组装和发布替换；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_pbs.py](../app/tests/test_pbs.py) | 验证PBS 资产选择及 SDK 布局；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_config.py](../app/tests/test_config.py) | 验证应用配置默认值与校验；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_qt_prune.py](../app/tests/test_qt_prune.py) | 验证Qt 模块、插件和运行库裁剪；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_startup_bytecode.py](../app/tests/test_startup_bytecode.py) | 验证目标标准库字节码预编译；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_third_party.py](../app/tests/test_third_party.py) | 验证wheel 解包、路径安全与导入发现；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_trim.py](../app/tests/test_trim.py) | 验证删除规则、keep 优先级及裁剪预设；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [app/tests/test_verify.py](../app/tests/test_verify.py) | 验证静态检查及目标运行时 smoke；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。 | 文件内中文说明 |
| [tools/__init__.py](../tools/__init__.py) | 定义 tools 包的导出边界；保留导出对象身份，避免在包初始化时引入额外运行副作用。 | 文件内中文说明 |
| [tools/build.py](../tools/build.py) | 解析构建命令并协调候选包验证、签名与发布；构建写入使用工作区互斥。 | 文件内中文说明 |
| [tools/desktop_probe.py](../tools/desktop_probe.py) | 在指定真实 Qt 平台测量窗口 exposed 并保存截图；不得把 offscreen 当成物理显示延迟。 | 文件内中文说明 |
| [tools/appbuild/__init__.py](../tools/appbuild/__init__.py) | 定义 tools/appbuild 包的导出边界；保留导出对象身份，避免在包初始化时引入额外运行副作用。 | 文件内中文说明 |
| [tools/appbuild/_vendor/__init__.py](../tools/appbuild/_vendor/__init__.py) | 定义 tools/appbuild/_vendor 包的导出边界；保留导出对象身份，避免在包初始化时引入额外运行副作用。 | 文件内中文说明 |
| [tools/appbuild/_vendor/tomli/__init__.py](../tools/appbuild/_vendor/tomli/__init__.py) | 定义 tools/appbuild/_vendor/tomli 包的导出边界；保留导出对象身份，避免在包初始化时引入额外运行副作用。 | 文件内中文说明 |
| [tools/appbuild/_vendor/tomli/_parser.py](../tools/appbuild/_vendor/tomli/_parser.py) | 解析 TOML 文档并报告语法错误；此文件随 tomli 上游维护，保留版权与兼容行为。 | 文件内中文说明 |
| [tools/appbuild/_vendor/tomli/_re.py](../tools/appbuild/_vendor/tomli/_re.py) | 解析 TOML 日期、时间和数字的正则匹配；保持时区与浮点解析的上游语义。 | 文件内中文说明 |
| [tools/appbuild/_vendor/tomli/_types.py](../tools/appbuild/_vendor/tomli/_types.py) | 定义 TOML 解析器的内部类型别名；不增加运行时业务依赖。 | 文件内中文说明 |
| [tools/appbuild/_vendor/tomli/py.typed](../tools/appbuild/_vendor/tomli/py.typed) | PEP 561 类型标记，必须保留原文件内容。 | 索引说明；保持格式或许可 |
| [tools/appbuild/appdir.py](../tools/appbuild/appdir.py) | 在配置导入前确定应用根目录；支持命令行、环境变量和默认示例路径。 | 文件内中文说明 |
| [tools/appbuild/build_info.py](../tools/appbuild/build_info.py) | 写入构建来源、源码摘要与有效选项；不记录原始配置中的密钥。 | 文件内中文说明 |
| [tools/appbuild/compat.py](../tools/appbuild/compat.py) | 适配宿主 Python 的标准库差异；宿主版本与被打包的目标 Python 版本独立。 | 文件内中文说明 |
| [tools/appbuild/config.py](../tools/appbuild/config.py) | 把应用 TOML 转为目标平台、ABI、路径和有效构建选项；路径以选定应用根为准。 | 文件内中文说明 |
| [tools/appbuild/egg.py](../tools/appbuild/egg.py) | 生成认证归档及加载器配置；打包内嵌密钥仅增加读取成本，不能承担授权安全。 | 文件内中文说明 |
| [tools/appbuild/egg_loader.py](../tools/appbuild/egg_loader.py) | 验证并加载应用归档；缓存需校验内容并持有租约，防止运行实例被淘汰。 | 文件内中文说明 |
| [tools/appbuild/fs.py](../tools/appbuild/fs.py) | 复制、散列和退役构建目录；复制内容时避免继承来源目录的限制性权限。 | 文件内中文说明 |
| [tools/appbuild/linux_release.py](../tools/appbuild/linux_release.py) | 将已验证候选目录交给 CPack 生成 Linux 安装包；全部请求格式成功后才发布归档。 | 文件内中文说明 |
| [tools/appbuild/lock.py](../tools/appbuild/lock.py) | 记录和检查 runtime、SDK 与 wheel 输入；依赖可重放不等于产物字节级可复现。 | 文件内中文说明 |
| [tools/appbuild/manifest.py](../tools/appbuild/manifest.py) | 生成规范化文件清单并验证摘要；清单路径必须相对受控根目录。 | 文件内中文说明 |
| [tools/appbuild/native/__init__.py](../tools/appbuild/native/__init__.py) | 定义 tools/appbuild/native 包的导出边界；保留导出对象身份，避免在包初始化时引入额外运行副作用。 | 文件内中文说明 |
| [tools/appbuild/native/bundle.py](../tools/appbuild/native/bundle.py) | 补齐原生依赖闭包并重定位加载路径；只操作候选目录，保留平台系统库边界。 | 文件内中文说明 |
| [tools/appbuild/native/deps.py](../tools/appbuild/native/deps.py) | 解析 PE、ELF、Mach-O 依赖并解析加载路径；缺失非系统库时必须给出可定位错误。 | 文件内中文说明 |
| [tools/appbuild/native/inspect.py](../tools/appbuild/native/inspect.py) | 按文件头识别原生格式与架构；不能只凭扩展名判断动态库。 | 文件内中文说明 |
| [tools/appbuild/net.py](../tools/appbuild/net.py) | 下载公开构建输入并处理可重试错误；日志与公开请求不应携带账户凭据。 | 文件内中文说明 |
| [tools/appbuild/package.py](../tools/appbuild/package.py) | 构建 launcher、组装候选包并生成发布清单；验证与发布替换由上层顺序协调。 | 文件内中文说明 |
| [tools/appbuild/pbs.py](../tools/appbuild/pbs.py) | 解析 PBS 运行时和 SDK 资产并验证解包布局；宿主工具与目标产物不可混用。 | 文件内中文说明 |
| [tools/appbuild/python_build.py](../tools/appbuild/python_build.py) | 执行 Cython 转换、原生编译和应用暂存；缓存键覆盖内容输入，依赖文件用于跟踪外部声明。 | 文件内中文说明 |
| [tools/appbuild/qt_prune.py](../tools/appbuild/qt_prune.py) | 根据应用导入与显式配置裁剪 Qt 负载；动态导入与 QML 插件需显式保留。 | 文件内中文说明 |
| [tools/appbuild/runtime.py](../tools/appbuild/runtime.py) | 选择 runtime 提供方并准备目标解释器；只裁剪复制树，不破坏共享原始 runtime。 | 文件内中文说明 |
| [tools/appbuild/signing.py](../tools/appbuild/signing.py) | 按镜像类型签署 Mach-O 与嵌套 bundle；扩展入 egg 前签名，应用封签在最后。 | 文件内中文说明 |
| [tools/appbuild/third_party.py](../tools/appbuild/third_party.py) | 按目标 ABI 获取 wheel 并安装到暂存目录；跨目标宿主 marker 求值存在边界。 | 文件内中文说明 |
| [tools/appbuild/toolchain.py](../tools/appbuild/toolchain.py) | 选择原生 MSVC、Unix 或 Zig 工具链；编译器架构与目标 Python ABI 必须一致。 | 文件内中文说明 |
| [tools/appbuild/trim.py](../tools/appbuild/trim.py) | 应用声明式删除与保留规则；keep 优先，预计算祖先集合避免重复扫描子树。 | 文件内中文说明 |
| [tools/appbuild/vendor/memimporter/MIT-License.txt](../tools/appbuild/vendor/memimporter/MIT-License.txt) | 第三方 MIT 许可原文，不翻译替换法律文本。 | 索引说明；保持格式或许可 |
| [tools/appbuild/vendor/memimporter/MPL2-License.txt](../tools/appbuild/vendor/memimporter/MPL2-License.txt) | 第三方 MPL 2.0 许可原文，不翻译替换法律文本。 | 索引说明；保持格式或许可 |
| [tools/appbuild/vendor/memimporter/MemoryModule.c](../tools/appbuild/vendor/memimporter/MemoryModule.c) | 在 Windows 内存中映射 PE 镜像并处理重定位、导入和资源；第三方实现保留 MPL 原文与出处。 | 文件内中文说明 |
| [tools/appbuild/vendor/memimporter/MemoryModule.h](../tools/appbuild/vendor/memimporter/MemoryModule.h) | 声明内存 PE 加载器句柄和回调；加载与释放必须使用配对接口，遵守上游许可。 | 文件内中文说明 |
| [tools/appbuild/vendor/memimporter/MyLoadLibrary.c](../tools/appbuild/vendor/memimporter/MyLoadLibrary.c) | 把系统 DLL 与内存 DLL 统一为加载接口；链表引用计数负责共享模块的释放时机。 | 文件内中文说明 |
| [tools/appbuild/vendor/memimporter/MyLoadLibrary.h](../tools/appbuild/vendor/memimporter/MyLoadLibrary.h) | 声明混合 DLL 加载入口；参数和句柄类型必须与 C 实现保持 ABI 一致。 | 文件内中文说明 |
| [tools/appbuild/vendor/memimporter/_memimporter.c](../tools/appbuild/vendor/memimporter/_memimporter.c) | 桥接 CPython 扩展导入与内存 DLL 加载；涉及解释器内部接口，升级 Python 时需原生回归。 | 文件内中文说明 |
| [tools/appbuild/vendor/memimporter/actctx.c](../tools/appbuild/vendor/memimporter/actctx.c) | 保存并激活 Windows DLL 的激活上下文；扩展加载后恢复原上下文，避免串扰。 | 文件内中文说明 |
| [tools/appbuild/vendor/memimporter/actctx.h](../tools/appbuild/vendor/memimporter/actctx.h) | 声明激活上下文函数指针及共享状态；运行时检查可用性后才调用系统 API。 | 文件内中文说明 |
| [tools/appbuild/verify.py](../tools/appbuild/verify.py) | 检查清单、架构与依赖并执行目标 import 和 launcher smoke；成功范围以实际运行步骤为准。 | 文件内中文说明 |
| [tools/appbuild/workspace_lock.py](../tools/appbuild/workspace_lock.py) | 通过操作系统文件锁拒绝并发写入；退出后由系统释放锁，锁文件本身可以保留。 | 文件内中文说明 |
| [tools/requirements.txt](../tools/requirements.txt) | 构建工具的 Python 依赖约束；目标应用依赖由应用配置独立声明。 | 文件内中文说明 |
| [docs/COMMENTING_GUIDE.md](../docs/COMMENTING_GUIDE.md) | 中文注释格式、所有权与验证规则。 | 文件内说明 |
| [docs/FILE_MAINTENANCE_INDEX.md](../docs/FILE_MAINTENANCE_INDEX.md) | 全文件维护说明索引；新增文件时同步登记。 | 文件内说明 |
| [tools/check_comments.py](../tools/check_comments.py) | 只读检查代码中文说明与索引覆盖；不能替代语义审查。 | 文件内说明 |
| [app/tests/test_cli_lock_help.py](../app/tests/test_cli_lock_help.py) | 验证构建占锁时帮助查询仍可用。 | 文件内说明 |
