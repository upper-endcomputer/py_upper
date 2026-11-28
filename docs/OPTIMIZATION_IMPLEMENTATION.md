# 优化整改实施记录

日期：2026-11-28；基于 `6676220` 工作树整改，代码已提交并推送 `develop`（`5332250`、`db6154c`），未发布新版本。
对应评估：[OPTIMIZATION_REVIEW.md](OPTIMIZATION_REVIEW.md)。该评估是整改前的快照，当前状态以本记录为准。

## 按原顺序完成的代码整改

| 项目 | 实现 | 验证 |
| --- | --- | --- |
| P1-01 Cython 缓存 | 模块绝对路径、应用内声明和头文件内容进入转换键；原生编译键补充编译器、显式 include/library 输入摘要 | 实际 Cython 3.2.9：仅改 pxd 会重新转换，增量输出与强制重建一致；头文件变更回归 |
| P1-02 候选包验证 | CLI 在 assembly 检查 static、runtime smoke、launcher smoke，通过后才调用发布替换；非 Linux release 先签名、公证和签名后 smoke；Linux 安装包生成也先于应用替换 | 静态、runtime、launcher、签名四种故障注入都不调用发布；真实本地 E2E |
| P1-03 macOS 公证 | 入 egg 前用正式 identity 签扩展；包内按 Mach-O 文件头发现无后缀镜像，嵌套 framework/app/xpc 与应用封签；ditto ZIP 提交、检查 Accepted、staple/validate | 命令契约、Rejected 状态、无后缀镜像测试；正式身份和 macOS 真机验证仍待执行 |
| P2-04 清单范围 | 非 Linux 默认只扫描当前应用；清单写在应用包外，避免破坏签名；Linux 保持目标归档目录 | 旧应用混入回归；清单现有测试 |
| P2-05 生命周期 | 并发 aclose 共享单个关闭任务，shield 防止一个等待者取消关闭；容器逐项尝试所有 close/dispose 后汇总错误 | 并发关闭、取消等待者、清理失败、重复停止 |
| P2-06 plist | 使用 plistlib 序列化 | 中文、空格、&、尖括号往返 |
| 工作区写入冲突 | CLI 持有 OS 文件锁，锁放在 `.tmp/build.lock`；退出或崩溃由 OS 释放；--app 子进程选择发生在加锁前 | 第二写入者被拒绝，释放后可重入；E2E build/lock/locked |
| 运行缓存 | 新 launcher 按应用 identifier 的 SHA256 前缀划分命名空间；Python/C++ 加载期间持有共享 lease，淘汰仅在取得独占 lease 后执行 | Python 保活/退出淘汰测试；编译真实 C++ helper 验证同样行为；缓存原有 63 个测试 |
| launcher 增量 | 保留 CMake/Ninja 树，工具链签名改变时清理；内嵌源码和 Windows 资源内容不变则不改 mtime；工具链环境只落摘要 | 文件 mtime 回归、现有 launcher 配置测试、真实 E2E |
| trim 遍历 | 一次遍历计算 keep 祖先集合，替代反复扫描每个候选子树 | 原有 trim 回归；深层 keep 实测删除集合相同 |
| 启动与构建观测 | `build/timings/<target>.json` 记录阶段秒数；`APPBUILD_STARTUP_TIMING=1` 启用 launcher 阶段毫秒输出 | C++ 编译与 E2E；不把 application-run 计时冒充窗口首帧 |
| 构建追溯 | 成品 `build-info.json`（macOS 位于 Contents/Resources） 记录 builder commit/dirty、源码与配置摘要、Python/target 和有效 egg/trim 选项 | 全套回归与最终 E2E；不保存原始配置密钥 |
| 跨目标依赖 | 宿主解释器回退时明确打印 marker 语义风险 | 保留原生解析策略及回归；目标原生 lock/wheelhouse 是推荐发布输入 |
| 测试归属 | 18 个原有构建测试文件原样迁到 `app/tests`，共享 conftest 移到根；pytest 默认收集两棵树，CI/VS Code 同步 | 逐文件与 Git HEAD 内容对比相同；全套测试通过 |
| 文档 | 修复 clean/build 操作、模板与配置默认差异、示例依赖准备、持久缓存注释、旧 HISTORY 当前版本文字、发布顺序和清理语义 | 文档核对、diff 空白检查 |

## 操作与兼容性

```bash
# 全部测试（默认仍跳过需要显式打开的本地 E2E）
python -m pytest -q

# 本地 E2E 新位置
APPBUILD_E2E=1 python -m pytest app/tests/test_e2e_local.py -q

# 改名后的全量重建，clean 本身不构建
python tools/build.py --clean
python tools/build.py

# 打包 launcher 分段计时：正常启动无需设置
APPBUILD_STARTUP_TIMING=1 APPBUILD_NO_DIALOG=1 dist/Upper/Upper
```

应用 identifier 应在不同产品间唯一；相同 identifier 被视为同一个应用缓存空间。新命名空间不会主动删除旧版无命名空间的缓存。lease 文件必须保留以维持跨进程锁的 inode/句柄身份，少量空锁文件不随负载目录删除。活跃版本可能使负载缓存暂时超过“两版”，这是保护运行实例所需的行为。

打包模式 `all/directory/none` 和 Qt Essentials/full 的选择仍由应用决定；没有把建议硬编码成通用默认值。已有安全默认、归档摘要验证和元数据保留策略不变。Cython 使用编译器 depfile 跟踪实际声明内容，包含应用目录外依赖；修改无关声明内容不会触发重编译。应用内声明文件新增或删除仍保守失效，以覆盖解析路径变化。外部搜索路径中新增同名声明造成的遮蔽尚未纳入目录清单，改变此类搜索环境时应关闭增量或清理构建缓存。

`build-info.json` 用于追溯，不等于所有构建工具已完全锁定或产物字节级可复现。工作区互斥是短期并发策略，同一工程不会并行构建多个应用；独立工作区可并行。库级函数调用者负责自己的同步。

发布仍有两次 rename 的短暂空窗，不保证断电事务恢复。Linux 归档目录与应用目录是两个发布路径，各自保留原有 rename 恢复逻辑，并非跨目录原子事务。Windows/macOS 清单位于 dist 下的 `<应用目录名>-release-manifest.json`。

## 实测

- 完整回归：**487 passed、6 skipped**；见 `.tmp/review/verified-suite.log`。随后调整 macOS build-info 位于 Contents/Resources，并通过最终边界回归 **18 passed**（`.tmp/review/final-boundaries.log`）。
- 推送前最终完整回归：**488 passed、6 skipped**；见 `.tmp/ci/prepush.log`。
- 最终真实本地 E2E：**3 passed，105.70 秒**，覆盖构建、目标/launcher smoke、native 输入、lock/locked 和迁移启动；日志 `.tmp/review/e2e-final.log`。
- 实际 PXD 验证：`incremental_changed=true`、`matches_clean_rebuild=true`，日志 `.tmp/review/pxd-fixed.log`。
- keep 子树基准：45 层目录，1,350 个待删除文件和一个最深层 keep 文件，复制时间不计入；旧实现三次约 0.987 秒，新实现 0.256/0.224/0.218 秒，删除数与保留集合一致。日志 `.tmp/review/trim-benchmark.json`。该结果只说明这个深层 keep 场景，不能外推为整体打包提速。
- C++ 缓存 lease 专项编译运行：**1 passed**，日志 `.tmp/review/lease-native.log`。

## CI 验收

- 首次提交 `5332250` 的 [CI 46151161484](https://github.com/upper-endcomputer/py_upper/actions/runs/46151161484) 暴露了 C++ 缓存 lease 测试夹具的跨平台问题：Windows 在编译探针时失败，macOS 在运行探针时失败；两个 Linux 原生 job 和六个 Python 兼容 job 通过。
- `db6154c` 将夹具使用的极端最小文件时间改为当前时间减一天，并直接输出子进程退出码、stdout 和 stderr，避免长 traceback 截断关键诊断。缓存保活与释放后淘汰的断言保持不变。
- 修复后的 [CI 46151741375](https://github.com/upper-endcomputer/py_upper/actions/runs/46151741375) 对应完整提交 `db6154c5ff30da722dfd23d1904371500f6acc91`，**15/15 job 成功，无失败、无跳过 job**：Python 3.8–3.13 六个兼容 job、八个原生集成 job（七种目标加 Windows Python 3.8 兼容构建）、一个七目标产物汇总 job。
- 八个原生 job 均通过单元测试、本地 wheel E2E、doctor 和完整集成构建。Linux x86_64、Windows x64/ARM64、macOS Intel/ARM64 以及 Windows Python 3.8 兼容构建执行 `--run` 并验证 `GUI OK`；Linux ARM64、Windows x86 不执行 GUI run。
- CI 设置 `APPBUILD_HEADLESS=1`，所以 `GUI OK` 是打包后应用的 readiness 证据，不等于交互桌面首帧验收。矩阵没有调用正式签名、公证或目标发行版安装流程。

上述本地 `.tmp/` 日志不随仓库提交；远端 job 的持久证据以链接所指的 GitHub Actions 运行记录为准。

## 平台验收边界

| 验收项 | 本轮状态 |
| --- | --- |
| 本机单元、真实 C++ helper、实际 Cython、Linux 本地 E2E | 已执行 |
| Windows 原生构建、共享句柄缓存 lease、GUI | CI x86/x64/ARM64 原生 job 通过；x64/ARM64 和 Python 3.8 兼容构建通过 headless GUI readiness |
| macOS Intel/ARM64 与 Linux x86_64/ARM64 原生构建 | CI 均通过；macOS 双架构与 Linux x86_64 通过 headless GUI readiness |
| macOS Developer ID、notarytool 服务、staple 后新机启动 | 代码与命令契约已修正；缺 macOS 与签名身份，未宣称通过 |
| X11/Wayland/Windows/macOS 真实桌面首帧 | 本轮没有显示会话实测；原 desktop_probe 仍测 source exposure |
| 目标发行版安装/升级/卸载 | 本轮未执行，原 CI 仍不能代替这些验收 |
| all/directory/none 与 Qt 输入的真实业务性能比较 | 观测接口已提供；未按一个示例的结果改变所有应用默认值 |

官方契约参考：[Apple 公证流程](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow)、[Cython 声明依赖](https://docs.cython.org/en/latest/src/userguide/sharing_declarations.html)。

## appbuild 迁移续接验收

- 构建器、配置和环境接口统一为 `appbuild`，测试统一到 `app/tests`；不保留旧接口兼容。SHA-256、Windows import library 与签名编排复用现有模块。
- Python 源码 1,174 个函数具备中文 docstring 或紧邻说明（嵌入 egg loader 使用注释以免破坏生成字符串）；逐文件维护检查 132 项通过，文档本地链接无缺失，工具源码 Python 3.8 语法检查通过。
- 全量回归：498 passed、6 skipped；显式真实 E2E：3 passed。Windows 内存加载器原生探针先复现无名称序号导出失败，修复后通过。
- Linux ARM64 正式 CLI `tools/build.py --run` 完成编译、打包、52 个原生文件检查及 launcher smoke；Qt 输出 `GUI OK offscreen`，退出码 0。本结果不代表真实桌面首帧或其他平台实机验收。
- 远端 CI 以本次提交对应 GitHub Actions 记录为准；历史基准 JSON 保留原始输出，不随接口重命名改写测量证据。
