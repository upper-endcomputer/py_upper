# 优化收尾实测（2026-11-28）

环境：Linux aarch64，目标 CPython 3.13.5，Cython 3.2.9，示例依赖 PySide6 6.12.0。测试应用复制到 `.tmp/closeout/app`，产品名 UpperCloseout，未修改用户本地应用配置。

## 构建与启动

每种模式首次构建后，执行无改动构建，再给 main.py 增加常量后构建。首次构建复用已有 runtime/wheel/Cython 缓存，不是无缓存全量基准。三种模式顺序运行，因此首次耗时不可直接比较。构建每项一次；启动冷/热各五次，p95 使用 nearest-rank（五次时等于最大值）。启动时间为进程启动到 headless GUI OK 后退出，包含应用执行；不是真实桌面首帧。冷缓存仅清空应用解包缓存，未清操作系统页缓存。

| 模式 | 无改动构建 s | 单文件修改 s | 冷启动 median/p95 ms | 热启动 median/p95 ms | 安装树 bytes |
| --- | ---: | ---: | ---: | ---: | ---: |
| all | 17.94 | 54.56 | 229.8/313.2 | 228.8/239.7 | 272460531 |
| directory | 16.96 | 25.64 | 254.2/395.3 | 219.2/303.8 | 272551556 |
| none | 17.77 | 19.95 | 248.0/326.0 | 217.7/219.3 | 271990733 |

无改动三组均复用全部 native 产物，Ninja 报 no work。单文件修改时 all 重建一个包含18模块的 bundle，directory 重建一个包含2模块的 bundle，none 重建1/25个扩展。原始数据保留实际编译阶段报告，没有把日志未打印的命令数当作零。

这个样本支持开发时按需使用 directory/none；启动差异样本有限，不能据此修改所有应用的发布默认值。

## Qt 与 Linux 发布

同一 none 模式换用 PySide6-Essentials 6.12.0 后安装树为 271764225 bytes，full 输入为 271990733 bytes。现有 Qt 裁剪已使两者成品接近，Essentials 主要减少下载/解包的非必需模块；不把 wheel 输入节省冒充成品节省。

TGZ 与 DEB 均生成成功。DEB 解包到项目临时目录后 launcher 返回 GUI OK。构建时显式声明 Debian 系统依赖，未把它当作自动依赖推导的验收。系统安装/升级/卸载未执行：Docker daemon 不可用，且当前操作限定在项目目录内。

## 未关闭的验收项

- 正式 macOS Developer ID 签名、公证、staple 后干净机启动：没有 macOS 执行环境与身份。
- Windows/macOS/X11/Wayland 真实桌面首帧：当前无显示会话，headless 不能替代。
- 目标发行版系统安装、升级、卸载：需要可用隔离系统。
- 外部依赖搜索目录新增同名声明的遮蔽：depfile 覆盖已解析依赖内容；新增解析候选尚未完整追踪。

## 复现与证据

[原始采样](benchmarks/2026-11-28-linux-arm64.json)、[基准脚本](benchmarks/reproduce.py)。在仓库根运行脚本；需要本地 app 配置、目标 runtime 和可用 Qt 系统依赖。脚本使用 .tmp/closeout，产物 UpperCloseout；同一工作区不能同时运行 build/pytest。临时完整日志位于 .tmp/closeout，未入库。
