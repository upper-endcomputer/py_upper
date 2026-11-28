# Linux 发布与异步应用

## 正式发布入口

```bash
python tools/build.py --target linux-arm64 --release
# 显式选择格式，可重复指定
python tools/build.py --target linux-arm64 --release --linux-format TGZ --linux-format DEB --linux-format RPM
```

默认 TGZ + DEB；生成到 `dist/releases/linux-arm64/`，包含本次归档的 SHA256 清单。只有全部格式成功才替换该目标的发布目录，失败保留上一次成功发布。历史 dist 根目录归档不属于这个入口的输出，也不应继续分发。

依赖 CPack；DEB 还需要 file、dpkg-shlibdeps（dpkg-dev）；RPM 需要 rpmbuild。Debian 使用 shlibdeps 生成系统库依赖，RPM 使用生成器自动依赖检测。跨发行版可用性仍须在目标发行版安装验证。当前版本只接受数字点分版本及 Linux 包名字符，不自动篡改版本。包安装到 `/opt/<小写应用名>`，提供 `/usr/bin` 链接及 desktop 文件。TGZ 也是安装目录树，可解压后直接运行其中 opt 下的程序。

参考：[CPack DEB](https://cmake.org/cmake/help/latest/cpack_gen/deb.html)、[CPack RPM](https://cmake.org/cmake/help/latest/cpack_gen/rpm.html)。生成包不等于已经通过系统安装/卸载测试。

## 异步生命周期

原同步入口保持可用；异步业务选择 `application.run_async()`。应用依赖需要加入 `qasync`，本轮验证版本为 0.28.0；headless 平台环境仍须在构造 QApplication 前设置。实现基于 [qasync QEventLoop](https://github.com/CabbageDevelopment/qasync)。

在初始化钩子中显式注册任务归属和服务关闭：

```python
owner = self.async_lifecycle
owner.register_close(device.aclose)  # 也接受同步 close
command = AsyncRelayCommand(device.read, task_owner=owner)
owner.create_task(background_receive())
```

退出时取消全部受管任务并等待其 finally，再逆注册顺序关闭服务（同步或异步），最后 stop 容器。关闭失败继续调用其余服务关闭方法，最终抛出错误。主动退出使用适配器 `quit()`；最后一个窗口关闭也触发该流程。不要直接调用 QApplication.quit 绕过异步退出，也不要在关闭回调中创建新任务。任务必须配合取消；当前不强制超时杀死阻塞驱动。未交给 owner 的第三方任务不在此生命周期内。

ServiceLocator 仍只负责依赖查找，不猜测任意对象的 close 方法；资源服务必须显式注册。CAN/Serial/OTA 的数据模型未扩展成真实硬件协议，实现驱动后接入上述契约。

## 桌面验证入口

使用安装有应用 Qt 依赖的解释器：

```bash
python tools/desktop_probe.py --platform xcb --output build/desktop-xcb
python tools/desktop_probe.py --platform wayland --output build/desktop-wayland
# Windows / macOS 上分别使用 windows / cocoa
```

输出 screenshot 与 JSON；严格指定平台，不回退 offscreen。计时从源码模块导入前到窗口 exposed，包含轮询误差，不是显示器首像素延迟，也不是打包 launcher 的进程启动时延。Windows/macOS、X11/Wayland 均需相应真实会话；Xvfb 只能增加虚拟 X11 覆盖，不能替代桌面实机结果。

当构建 sysroot 不在宿主 dpkg 数据库中，或 PBS 的 `$ORIGIN` 库引用不能由 dpkg-shlibdeps 解释时，可用 `--linux-deb-depends 'libc6 (>= ...), ...'` 显式声明目标发行版依赖。该选项关闭自动扫描，声明必须由发布方核对，不能传空串。当前 ARM64 Qt wheel 的基线为 glibc 2.39；本轮声明及完整命令保存在发布验证报告，不能复制到其他架构充当通用依赖表。

## 整改后的验证与关闭契约

CLI 在候选包完成静态与 smoke 验证后发布，Linux 安装包生成失败也不会替换当前应用目录。异步关闭请求共享同一个关闭任务；等待者取消不会取消实际关闭。容器清理会继续尝试其余 View/ViewModel，最终报告错误。实现状态和平台验收矩阵见 [优化实施记录](OPTIMIZATION_IMPLEMENTATION.md)。
