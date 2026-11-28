# py_upper 深度梳理与优化评估

评估日期：2026-11-28。基线：`6676220`，文档与配置模板版本 `0.17.37`。

本轮交付是评估和复现证据，未修改生产代码。优先级：P1 为可能产出错误结果或阻断发布；P2 为可靠性、工程效率与使用体验。实测、静态确认、待测推断分别标明。

## 1. 项目定位与值得保留的设计

这是自带 CPython 的跨平台应用构建与交付工具，包含三层：

```text
应用配置 + src + resources
    → Target / runtime / SDK / wheels
    → Cython 单模块转换 → native 单模块或合并 bundle
    → 可选 egg → launcher → 目录组装 / native 闭包 / 裁剪
    → 发布目录 → 静态校验 → runtime smoke → launcher smoke
    → 可选实际运行 / 平台签名与安装包

最终用户启动：launcher → bundled CPython → 应用入口
示例应用内部：Qt 适配器 → MVVM 容器 → View / ViewModel / Model / Service
```

第三层是示例，不是打包工具的强制运行依赖。硬件 CAN/Serial/OTA 目前是示例数据模型，不能把它们当成已有通信框架。

应保留的基础：宿主与目标 Python 分离；Target 统一 ABI；第三方 wheel 不 Cython 化；native 闭包缺失时报错；对复制树裁剪；默认保留分发元数据；操作系统定位安装路径；只读安装目录；中文路径与迁移 E2E；fake Qt 和真实加载验证分开。

当前最有价值的工作是补齐构建、验证、发布之间的保证，并使增量缓存可解释。进一步缩小体积和启动加速应建立在测量之上。

## 2. 优先修复的具体问题

### P1-01：Cython 缓存漏掉声明依赖，可能悄悄交付旧代码

- 证据：`tools/py_upper/python_build.py:235` 的 `_cython_key()` 仅包含 schema、Cython 版本、源文件名和源文件内容。
- 实测：`demo.py` 不变，把 `demo.pxd` 的 `cpdef int f(int x)` 改成 `cpdef double f(double x)`；第二次构建显示 `1 module(s) up to date`，生成 C 与旧版本完全一致；关闭增量后重新生成的 C 发生变化。
- 影响：增量构建成功不等于实现与输入一致；切换外部应用时，共用缓存目录也会放大这类相同源码、不同声明依赖的风险。
- 最小修复方向：跟踪实际读取的 `.pxd/.pxi` 依赖及内容，纳入转换键；同时记录工具版本、编译指令和模块身份。可以先用保守失效策略保证正确性，再优化依赖粒度。
- native 层补充：`_native_key()` 包含命令与 C 内容，但同一路径下编译器、SDK 头文件或链接库内容更新不会因路径本身变化而失效。这是静态确认的键覆盖缺口，本轮未做真实 SDK 更新复现。
- 验收：依赖变化触发对应重编译；无关文件变化不误伤；缓存产物与干净构建结果在语义上相同。

### P1-02：校验与 smoke 在发布替换之后，失败不能保住旧包

- 证据：`tools/build.py:200` 先 `package()`，之后才 `verify()` 与两层 smoke；`package.py:311` 的 `_publish()` 发布后立即清理包含旧包的 assembly。
- 实测：调用发布函数后，新内容已在 destination，旧备份所在 assembly 已不存在。结合 CLI 调用顺序，后续校验失败没有旧包可回滚。
- 文档已经承认该限制；它不是本轮新出现的回归，但应是交付工具下一阶段的核心改进。
- 最小修复方向：允许 verify/smoke 显式接收候选包根，在 assembly 完成校验后再发布；或者保留 previous 到全部检查成功。签名后的验证也应纳入发布边界。
- 不要把 rename 异常恢复描述为断电恢复；需要断电恢复时，再增加简单事务记录和启动恢复规则。
- 验收：分别注入静态失败、import 失败、launcher 失败、签名失败，旧成品仍可用；成功后才清理备份。

### P1-03：macOS 正式公证路径提交了不受支持的输入格式

- 证据：`tools/build.py:256` 向 `notarytool submit` 传入 `.app` 目录，未先生成 ZIP/DMG/PKG。
- Apple 官方说明要求以受支持归档提交，明确指出 `.app` bundle 不能直接上传。本项是代码与官方契约核对，未在本机执行 Apple 公证。
- 修复方向：签名完成后生成保留 bundle 结构的 ZIP，提交并检查 Accepted 状态，staple 原 app，验证后生成最终分发物。
- 另一个必须联测的接合点：release 只枚举散落 `.dylib/.so` 和 launcher；默认 egg 内的 merged 扩展不会被这一轮正式签名遍历到。当前代码也未传 library-validation entitlement。应选择并验证“入 egg 前同 identity 签名”等明确策略，不能用 ad-hoc smoke 代表 hardened release 已可启动。
- 验收：真机运行正式签名、公证、staple 的默认 egg 产物，并在无构建缓存环境启动。仅 mock 命令不能证明交付可用。

来源：[Apple Customizing the notarization workflow](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow)、[Apple Developer ID](https://developer.apple.com/developer-id/)。

### P2-04：发布清单边界过宽，可能把旧应用一起标成当前版本

- 证据：`package.py:415` 的 `write_release_manifest()` 默认扫描整个 `DIST`；非 Linux release 使用这个默认值。
- 实测：同一个 dist 中有 Current 和 OldApp，清单同时列出两者的 payload。
- Linux 新发布入口已经显式传本次 release 目录，可复用这个做法，无需重新设计清单系统。
- 修复与验收：各平台都传当前成品根；清单记录实际产物与有效配置摘要；在 dist 放旧应用和其他 target，确认不被收录。

### P2-05：异步关闭缺少并发幂等，同步清理缺少故障隔离

- 实测：对同一个 `AsyncLifecycle` 并发调用两次 `aclose()`，一个 async closer 被调用两次。`async_lifecycle.py:33` 只在进入时检查 `_closed`，第一次等待时第二次仍能进入。
- 修复方向：缓存并共享一个关闭任务，让多个调用方等待同一次关闭；定义取消传播策略，避免调用方取消导致资源只关闭一半。
- 静态边界：`mvvm_application.py:94` 的 View `close/dispose` 或 ViewModel `dispose` 任意抛异常，就跳过后续清理与注册表清空。文档已说明该限制；它与 AsyncLifecycle 的“一个服务失败继续关闭其他服务”语义不一致。
- 验收：并发关闭、重复关闭、任务异常、某个服务/视图关闭失败时，所有应释放对象仍按顺序尝试且每个只执行一次，错误最终可见。
- 这属于示例骨架完善，不应成为纯打包用户的额外依赖。

### P2-06：合法应用名未做 XML 转义

- 证据：`config.app_name()` 允许 `R&D`；`package.py:294` 用 f-string 手写 XML。
- 实测：`plistlib.loads(_mac_info_plist('R&D').encode())` 抛出 `ExpatError`。
- 最小修复：使用标准库 `plistlib` 序列化；不必禁止正常的产品名称。
- 验收：中文、空格、`&`、`<` 等值序列化后可以解析并还原；identifier 也做适合其用途的校验。

## 3. 需要纳入下一轮的工程边界

### 应用隔离与多进程构建

`--app` 已解耦源码输入，但 staging、wheel 目录、native 缓存仍主要按 target 命名，Cython 输出共用目录，LOCK 是仓库级单文件；launcher 构建也会直接删除该 target 的构建目录。

这意味着“能够切换应用”还不是“多应用可并行构建”。并发时目录读写互相干扰是从实现推导的风险，本轮未跑并发破坏实验。短期给工作区构建加互斥并清晰报错；确有多应用需求时，再引入 app identity/target/config digest 目录隔离。不要一上来全面重构为插件框架。

锁文件固定了 runtime/SDK/wheel，不等于所有构建输入都固定。Cython 范围版本、编译器、SDK 内容、有效裁剪选项和源版本等，应逐步写入 build-info。要区分依赖可重放与字节级可复现，不能仅凭 `--locked` 宣称后者。

### 跨目标 wheel 解析的可信度

`third_party.resolution_python()` 原生目标优先目标解释器；交叉目标退回宿主 pip 加目标 tags。项目文档明确写出 marker 仍按宿主求值的限制。

建议先让构建报告显式区分“目标解释器解析/宿主回退”，交叉发布优先消费目标原生 CI 生成的锁与 wheelhouse。不要把宿主回退的成功下载当成目标依赖完整；复杂跨平台 marker 解析器暂不值得自建。

### 运行期缓存归属与存活实例

Python `_prune_cache()` 和 C++ `prune_bundle_cache()` 都按全局根目录中的相应前缀保留两个条目，没有应用身份或使用中租约。三个不同应用也可能互相挤掉缓存；POSIX 上“文件已加载”不保证整棵目录删除失败，后续动态导入与资源读取值得验证。

本轮没有复现用户可见崩溃，不列成已确认故障。建议以三个应用、旧版本长驻、新版本启动、延迟导入构造验证；确认需求后做按应用分区和使用中保护。关闭缓存摘要校验不解决该问题。

## 4. 性能优化顺序

| 方向 | 当前证据 | 建议 | 验收指标 |
| --- | --- | --- | --- |
| launcher 增量 | `build_launcher()` 每次先删目录再 CMake configure/build | 保留 CMake/Ninja 构建树，生成文件内容变化才写回；SDK/target/编译配置变化明确失效 | 无改动构建耗时、实际编译命令数 |
| Cython 合并粒度 | `all` 只加载一个 bundle，但修改任何组内模块会重新链接整个 bundle | 对实际应用比较 `all/directory/none`，开发可选 directory，发布按测量选 | 单文件修改重建、冷/热启动、产物大小 |
| keep 子树扫描 | `trim.py:328` 对待删除目录重复 `os.walk`；深层保留项会让祖先多次扫描 | 单遍生成保留祖先集合，再复用；仅在大目录测得瓶颈后实施 | 遍历项数、裁剪时长、删除集合一致 |
| Qt 输入体积 | 已有按 import 与显式列表裁剪，动态 QML/import 有边界 | 应用只用 Essentials 时缩小声明依赖；按真实功能测试保留插件 | 压缩包、安装树、缓存三个体积分别记录 |
| 启动诊断 | `desktop_probe.py` 测源码导入到 exposed，不测 launcher 全链路 | 记录归档认证、缓存核验/解压、CPython 初始化、应用 import、窗口 exposed 分段耗时 | 同机冷缓存/热缓存各多次，报告中位数与 p95 |

现在没有本轮完整打包与真桌面的性能基准，因此不预测“还能快多少”或沿用历史平台数字作为当前收益。保留 `safe` 默认和缓存摘要验证，不通过删元数据/关校验制造表面性能提升。

`merge=all` 的 native bundle 即使处于 egg 内也会由 launcher 提前解压到缓存；它和 Windows 普通扩展的 memory 后端不是同一条装载路径。优化或产品说明应区分这两条路径。egg 定位仍是增加直接读取成本，密钥随 launcher 分发，不能承担授权或机密保管职责。

## 5. 文档与产品化整理

1. README 收敛为定位、最小上手、输出物、支持矩阵和文档入口；配置全集、历史测量、原理与排错集中在专题文档，减少多处同步。
2. 修正可执行命令：README 改名说明要求执行 `--clean` 后“重新构建”，但 CLI 的 clean 分支只删除并返回。应明确先 clean，再 build；不能把 clean 当成重建。
3. 新克隆模板 dependencies 为空，而示例 GUI 需要 Qt。保留通用空模板，但给示例明确依赖准备步骤，或提供单独示例配置；避免把 CI 注入依赖的知识藏起来。
4. 拆出工具自身测试到根 tests/tools 等稳定位置，示例测试留在 app/tests。否则“可整体替换 app”会连构建器回归一起删掉。可分阶段迁移，不必改变框架。
5. 修正文档细节：README 的 qt 默认描述与配置缺省/模板默认混杂；模板 egg 注释还写 temporary directory，实际默认是持久内容缓存；HISTORY 后半部残留“current v0.16.16”声明。
6. 发布验证矩阵显式区分：静态、目标 import、launcher smoke、offscreen 窗口、真实桌面、正式签名/公证、安装升级卸载。当前 CI 构建 smoke 不能替代正式 release；Linux ARM64 的 GUI run 被关闭，Windows x86 也不跑 GUI。
7. 工具版本与应用版本现在在示例工程中混用。自有应用应有自己的产品版本；构建 provenance 单独记录构建器 commit，先解决追溯，不急着增加一套版本框架。

## 6. 推荐实施批次

- 第一批：PXD 缓存失效、候选包先验证后发布、macOS 公证格式与正式签名启动闭环。这三项决定是否交付正确代码、是否保住旧版本、是否能正式分发。
- 第二批：发布清单边界、plist 序列化、并发关闭与清理错误隔离，配套边界回归。
- 第三批：launcher 增量、真实构建/启动指标；有数据后再决定 merge 粒度、trim 扫描和 Qt 输入优化。
- 第四批：整理文档与示例测试边界，按实际并发需求引入工作目录隔离。

不建议此时重写 launcher、扩大自定义密码格式、增加通用插件系统或把示例硬件模型升级成完整通信平台；这些不是当前问题的最短解。

## 7. 本轮验证与限制

- 选定现有测试：incremental_build、merged_build、package、linux_release、async_lifecycle、trim、manifest，共 **74 passed，1.67 s**。
- 实际 Cython **3.2.9** 复现 PXD 依赖变更缓存失效缺口；另外复现并发 close 重复、plist XML 无效、清单混入旧应用、发布后旧备份删除。
- 日志：`.tmp/review/pytest.log`、`.tmp/review/reproduce.log`；脚本：`.tmp/review/reproduce.py`。依赖安装在 `.tmp/review/venv`，临时文件均位于项目 `.tmp/`。
- 未执行完整构建、完整测试集、目标 OS 实机运行、远端 CI 查询或正式签名公证；以上未验证项目均不能从历史绿灯推断。
- 没有改动生产代码或现有测试，没有提交和推送。

复现命令（仓库根）：

```bash
PYTHONDONTWRITEBYTECODE=1 TMPDIR="$PWD/.tmp/review" \
  .tmp/review/venv/bin/python .tmp/review/reproduce.py

PYTHONDONTWRITEBYTECODE=1 TMPDIR="$PWD/.tmp/review" \
  .tmp/review/venv/bin/python -m pytest \
  app/tests/test_incremental_build.py app/tests/test_merged_build.py \
  app/tests/test_package.py app/tests/test_linux_release.py \
  app/tests/test_async_lifecycle.py app/tests/test_trim.py \
  app/tests/test_manifest.py -q -p no:cacheprovider \
  --basetemp="$PWD/.tmp/review/pytest"
```

历史记忆来源校验：Hindsight 的「py_upper 发布与异步生命周期」提供了此前 Linux 发布与异步生命周期工作的线索，当前代码确认两者存在；该共享记忆同时混入 ai_upper 重命名与 QML 信息，与本工作树不符，已记录项目范围纠正。本报告代码结论均以当前工作树和本轮证据为准。
