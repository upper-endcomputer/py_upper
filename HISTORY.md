## v0.17.24

0.17.23 的 macOS/Linux 全绿，但三个 Windows CI job 全部倒在 `Local end-to-end build with third-party wheel`，链接器报 `LNK2001: unresolved external symbol PyInit_py_upper__root`。问题不在 Cython 生成，也不在 register.c，而是 MSVC 路径多要了一个根本不存在的入口。

合并 bundle 的每个 Cython 单元都被重写成唯一的 `PyInit_py_upper_<模块名>_<摘要>`，真正的导出入口是 register.c 里的 `py_upper_register_modules()`，launcher 也是按这个名字解析的。但 `windows_merged_setup_script()` 走的是 setuptools：Windows 上的 `build_ext` 会按 Extension 名自动补 `/EXPORT:PyInit_<扩展名>`，除非覆盖 `get_export_symbols()`。于是 link.exe 把 `PyInit_py_upper__root` 当成必须存在的符号，找不到就 LNK2001，连 `.pyd` 都生成不出来。

macOS/Linux 和本机 macOS 交叉构建没有暴露，是因为 Unix 直接调 clang/gcc，Windows 交叉走 Zig，根本不经过 setuptools 的导出推断；只有 Windows 宿主上的原生 MSVC 路径会加这个参数。

修复是在生成的合并 `setup.py` 里覆盖 `TargetBuildExt.get_export_symbols()`，固定返回 `['py_upper_register_modules']`。register.c 本身仍带 `__declspec(dllexport)`，两条导出路径一致。新增测试直接断言生成的脚本包含这个覆盖，并且不再出现 `PyInit_py_upper`。

验证：全量 `python -m pytest app/tests -q` 为 290 passed, 5 skipped；合并构建测试 9 passed。MSVC 实际链接由 CI 的 `native-integration` Windows jobs 复验。

## v0.17.23

0.17.22 的 launcher 冒烟能过，但真正的 Windows 应用仍然一启动就退出，返回码 1，窗口根本没有出现。报错已经不是 `ModuleNotFoundError`，而是 `utils.paths.app_name()` 里的 `IndexError: 2`：合并 bundle 的导入问题修好了，应用自己的路径推断又挡住了启动。

第一个根因是 `__file__`。合并后的 Cython 模块通过 inittab 注册成 built-in 模块，CPython 给它的 spec origin 是 `built-in`，Cython 又把这个字符串复制成 `__file__`。`utils/paths.py` 直接算 `Path(__file__).resolve().parents[2]`，但 `built-in` 不是源码路径，也没有第三层父目录。macOS 上因为 launcher 导出的 `PY_UPPER_HOME` 可见，`app_name()` 和 `resource_root()` 提前返回，恰好没有走到这里。

第二个根因是 Windows 的环境变量可见性。launcher 用 `SetEnvironmentVariableW` 导出 `PY_UPPER_HOME`、`PY_UPPER_APP_NAME` 等值，这个 API 更新的是进程环境块，但不会刷新 CRT 里那份环境快照，而 Python 的 `os.environ` 正是从 CRT 快照构建的。结果应用读到的全是空值。POSIX 的 `setenv` 直接更新 `environ`，所以同一份代码在 macOS/Linux 上正常。现在 launcher 在设置原生环境变量的同时，把所有导出值收集成 `LAUNCHER_ENV` 字典，bootstrap 在加载 egg 和入口之前执行 `os.environ.update(LAUNCHER_ENV)`，Python 侧始终能看到这些值。

第三个问题出在崩溃报告本身：内置入口的 `__file__` 指向 `Upper.exe`，`traceback.print_exc` 为了显示源码行去解码 PE 镜像，抛出 `SyntaxError`，把真正的异常覆盖掉了。现在 bootstrap 把入口源码缓存进 `linecache`，报告可以直接显示入口代码，不再读 exe。

`utils/paths.py` 也改成了防御式实现：新增 `_module_file()` 和 `_project_root()`，只有 `__file__` 确实是存在的文件时才计算父目录；没有源码树时 `app_name()` 返回编译进 launcher 的默认名，`resource_root()` 回退到可执行文件所在目录。测试覆盖 `__file__ == "built-in"` 的情况，并断言 bootstrap 会重新发布 launcher 环境、缓存入口源码。

验证：全量测试 287 passed, 7 skipped；Windows x86_64 全量构建全部 PASS，launcher 冒烟通过。`PY_UPPER_HEADLESS=1 dist\Upper\Upper.exe` 返回 0，stdout 为 `Hello from py_upper` 和 `GUI OK offscreen`，stderr 为空。交互启动后进程保持运行，存在真实顶层窗口，标题为 `Upper`。
## v0.17.22

0.17.21 的合并 bundle 在 macOS（CPython 3.13）上启动正常，但 Windows x86_64（CPython 3.8.10）一启动就报 `ModuleNotFoundError: No module named 'mvvm.views.base_view'`。bundle 已经成功 `dlopen`，`_imp.is_builtin()` 和 `sys.builtin_module_names` 也都能看到这个名字，说明问题不在注册，而在查找。

根因是 CPython 3.8-3.12 的 `BuiltinImporter.find_spec()`：只要 `path is not None` 就直接返回 `None`，哪怕 `_imp.is_builtin(fullname)` 为真。合并模式把 `__init__.py` 保留成 `.py` 包标记，所以每个 bundle 叶子模块都是通过父包导入的，父包的 `__path__` 一定会作为 `path` 传进来。3.13 删掉了这个检查，macOS 因此掩盖了问题；Windows 仍用 3.8.10，问题就暴露了。

修复放在 Python 层的 `egg_loader.py`：新增 `BuiltinSubmoduleFinder`，只处理带点号的 built-in 名字，且只在 `path is not None` 时接管，然后以 `path=None` 调用 `BuiltinImporter.find_spec()`。`install()` 在 memory 和 extract 两条后端上都会安装这个 finder，位置在 egg importer 之后、`BuiltinImporter` 之前，所以 egg 里真实存在的散落模块仍然优先，合并 bundle 的点号模块也不会被 3.8 的旧检查挡住。测试覆盖 finder 的顺序、幂等性、只解析点号 built-in，并在用例结束后清理 `meta_path`。

验证：全量测试 286 passed, 7 skipped；Windows x86_64 全量构建 `READY: dist\Upper`，launcher 冒烟通过；`py_upper.dat` 内是 `_root`、`mvvm`、`utils` 三个合并 bundle，`dist\Upper\site-packages` 下没有 `py_upper_bundles` 目录。
## v0.17.21

0.17.20 把 egg 解压改成内容寻址缓存后，热启动回到了 0.6 秒，但冷启动还是由模块数量决定。原因是每个 Cython 模块都被编译成独立的 Python 扩展，也就是独立的 Mach-O 镜像；macOS 会逐个做 dyld 加载和代码签名校验。实测 24 个 `.so` 冷启动 8.76–8.90 秒，纯 `dlopen` 1001 个扩展要 305.6 秒。缓存能省掉重复解压，省不掉每个镜像的首次校验，所以“文件多”本身就是问题，不是“解压慢”。

这一版在 `[tool.py_upper.cython]` 下新增 `merge = "none" | "directory"`。`none` 保留旧布局；`directory` 按模块名的第一段分组，把同一个顶层目录下的叶子模块链接成一个 bundle。根目录模块（`main.py`、`application.py`）归到 `_root`，`__init__.py` 明确排除：内置模块不是包，包标记必须继续以 `.py` 存在，`from . import` 才能工作。当前应用 17 个叶子模块合并成 3 个 bundle：`_root` 2 个、`mvvm` 13 个、`utils` 2 个。

合并的难点是入口符号。Cython 为每个模块生成自己的 `PyInit_<module>`，直接链接会重名。构建时会把每个生成单元里的入口重写成唯一符号 `PyInit_py_upper_<sanitized>_<sha256[:12]>`，如果某个单元里找不到入口或出现多个入口就直接报错，不会产出一个看起来成功、实际导入不了的包。每组再生成一个 `register.c`，里面有静态 `_inittab` 和导出的 `py_upper_register_modules()`，由它调用 `PyImport_ExtendInittab`。这些改写后的单元放在 `build/merged/<target>/<group>/`，共享的 Cython 缓存不被原地修改；模块被删掉后旧单元也会清掉。bundle 的编译缓存单独使用 `py_upper-merged-native-1`，key 覆盖所有翻译单元内容、`register.c`、完整链接命令和 MSVC 环境，所以只改代码不改模块名也会失效。

`PyImport_ExtendInittab` 只允许在解释器初始化前调用，因此 bundle 不能等 Python 层的 egg loader 处理。现在 `py_upper_bundles/` 下的文件以 `ZIP_STORED` 写进 `bundle/py_upper.dat`，其他成员仍按配置压缩；launcher 新增自实现的 Keccak/SHAKE-256（`sha3.h`）和不依赖 zlib/lzma 的 ZIP 读取（`egg_bundles.h`），在 `Py_InitializeFromConfig` 之前解密、解析、取缓存 key（`shake256(payload)[:16]`），把 bundle 发布到 `merged-<32hex>` 内容寻址目录，再 `dlopen` 并注册。缓存写入沿用 staging + `.complete` + rename，路径与 0.17.20 的 egg 缓存同根：macOS 是 `~/Library/Caches/py_upper/eggs`，Windows 是 `%LOCALAPPDATA%/py_upper/cache/eggs`，Linux 是 `$XDG_CACHE_HOME/py_upper/eggs` 或 `~/.cache/py_upper/eggs`。`RuntimeGuard` 会先卸载 bundle 再卸载 libpython，避免析构顺序出错。Python 层的 egg loader 现在跳过 `py_upper_bundles/`，不会重复解压。

`--verify` 同时支持新旧两种布局。合并模式下检查 bundle 在 egg 内、`site-packages` 没有散落副本、entry 是 `ZIP_STORED`、架构匹配 target、包标记仍是 `.py`、每模块扩展不存在。普通解释器冒烟在合并模式下不再 import 应用入口，因为裸解释器没有 inittab 可扩展；入口由 launcher 冒烟覆盖。新增的 `test_merged_build.py` 覆盖分组、根模块、包标记、符号重写、零/多入口拒绝、bundle 存储、loader 跳过、架构检查和 C++/Python 两侧 egg reader 的一致性。

验证：全量测试 286 passed, 5 skipped；macOS arm64 构建 `READY: dist/Upper.app`，verify 的 `PASS packaged egg merged bundle _root/mvvm/utils`、`PASS packaged merged bundle not loose ...`、`PASS merged bundle stored ...`、`PASS arch bundle ...=arm64` 全部通过；`py_upper.dat` 941 KB、10 个 entry（3 个 bundle 加包标记），`dist/Upper.app` 内没有 `py_upper_bundles` 目录；headless 冷启动 1.22 秒、热启动 0.55 秒；`codesign --verify --deep --strict` 与 `git diff --check` 均通过。限制：合并只处理应用自己的模块，第三方 wheel 仍是原样；bundle 的缓存目录在首次启动时需要可写，否则 launcher 会直接报错而不是退回散文件。

## v0.17.20

上一版收尾时还剩四个问题。第一，`__init__.py` 是唯一没有进 Cython 的应用源码：源文件选择直接跳过了所有包标记，所以 `mvvm/__init__.py` 这类文件仍以可读源码躺在 egg 里。现在包标记也参与编译；`module_name()` 返回 `mvvm` 用于 include/exclude 匹配，`extension_name()` 保留 `mvvm.__init__` 供 setuptools 生成文件名和 `PyInit___init__` 入口，两者不再共用一个函数。测试同时断言包标记被选中以及两个名字各自的取值。

第二，macOS 双击启动慢。`extract` 后端每次都在新的 `py_upper_egg_*` 临时目录里解压，17 个 `.so` 对 dyld 来说每次都是首次出现，缓存元数据无法复用，冷启动约 6.6 秒。现在解压目录按解密后 zip 的 SHA-256 命名（`egg-<digest>`）：先在同级 `.egg-<digest>-*` 暂存目录写入并打 `.complete` 标记，再用 `os.replace` 原子发布；命中标记时直接复用，不再解压，也不再注册 `atexit.rmtree`。缓存根在 macOS 是 `~/Library/Caches/py_upper/eggs`，Windows 是 `%LOCALAPPDATA%/py_upper/cache/eggs`，Linux 是 `$XDG_CACHE_HOME/py_upper/eggs` 或 `~/.cache/py_upper/eggs`，`PY_UPPER_EGG_CACHE` 可覆盖；目录不可写时回退旧的临时目录。实测同一份 egg 冷启动 6.6–10.0 秒（受磁盘状态和首次签名校验影响），命中缓存 0.58–0.64 秒，约 10–17 倍。

第三，Windows 交叉构建暴露 `_memimporter` 在 CPython 3.13 上编译失败：3.13 删除了 `_Py_PackageContext` 和 `_PyImport_FixupExtensionObject`，`auto` 后端于是静默退回 extract。vendored `_memimporter.c` 现在按版本分支：3.13+ 不设置 package context（Cython 走 multi-phase，spec 自带模块名），单阶段模块直接写入 `sys.modules`；`do_import()` 返回模块对象，不再依赖 `PyImport_ImportModule`；multi-phase 始终调用并检查 `PyModule_ExecDef`，init 返回 NULL 也会报错。重新交叉构建后警告消失，`_memimporter.pyd` 正常生成并包含 `PyInit__memimporter`。

第四，窗口能创建但不会自动置前。`MainWindow.show()` 只调用了 `QMainWindow.show()`，Qt 只负责显示窗口，不会强制应用激活；冷启动一慢，LaunchServices 的激活就过期了，Finder 看起来像没反应。现在显示后补 `raise_()` 和 `activateWindow()`，测试里的 fake window 记录两个调用并在装配测试里断言。用 Finder 实际打开验证：冷启动 1.13 秒 check-in、1.70 秒内到前台；热启动 0.47 秒 check-in、0.69 秒到前台；「关于 MVVM 架构」按钮能正常打开第二个窗口。

验证：全量测试 276 passed, 5 skipped；macOS arm64 构建 READY、21 个原生二进制检查通过，headless 冷/热启动为 6.6–10.0 s / 0.58–0.64 s，Finder 实际打开冷启动 1.13 秒 check-in、热启动 0.47 秒 check-in；Windows x86_64 Zig 交叉构建 READY、420 个原生二进制检查通过。限制是 Windows 产物只在 macOS 上完成交叉编译和静态布局检查，没有在 Windows 宿主上实际运行。

## v0.17.19

0.17.18 之后，Windows 和 macOS 的产物里仍能看到第三方包的源码。真正能删的是 binding 自带的部署工具：PySide6 的 scripts/support/doc/docs/lib，shiboken6 的同名目录加 include。这些目录里大量是 `.py`，但运行时不会 import，import 闭包也看不见，所以按引用删模块/库/插件的逻辑对它们无效。serial 这类纯 Python 包保持 wheel 原样，不属于裁剪范围。

根因是 `tools/py_upper/trim.py` 的全局规则只点名了 Qt 的 examples/include。规则也不能写成 `**/lib`：`PySide6/lib` 是 CMake 元数据，而 `PySide6/Qt/lib` 是运行时库目录，递归匹配会把 Qt 本体删掉。这一版把规则锚定在 binding 根目录，新增 `PySide*/scripts|support|doc|docs|lib` 与 `shiboken*/scripts|support|doc|docs|lib|include`；`PySide6/Qt/lib` 和 `shiboken6/libshiboken6.abi3.so` 都保留。`app/tests/test_trim.py` 在现有用例里补了这些断言，模板的全局规则说明同步更新，版本号改为 0.17.19。

验证：`python -m pytest app/tests -q` 为 272 passed, 5 skipped（与 0.17.18 相同，新增的是既有用例里的断言）。macOS arm64 用当前默认值重建 `[1/6]`…`[6/6]` 通过，codesign 与 launcher 冒烟均过；`dist/Upper.app` 74 MB，PySide6 保留 7 个运行时 `.py` + 59 个 `.pyd`，shiboken6 3 个 `.py` + 1 个 `.pyd`，dist-info 为 0。Windows x86_64 Zig 交叉构建 `[1/6]`…`[6/6]` 通过，包体计数相同，dist-info 为 0。

第三方包原地 Cython 编译的方案实现并实测后删除了，工具不再提供 `packages`/`package_exclude`。pyserial 编译后从约 300 KB 涨到 7.9 MB（28 个 `.pyd`），PySide6/shiboken6 的原生模块本来就是编译好的，剩余 `.py` 只是运行时 shim；编译还会破坏 introspection、`python -m package`、monkeypatch 和 traceback 行号。限制是 PySide6 仍有 7 个运行时 shim、shiboken6 仍有 3 个，删掉会导致 import 失败；Windows 交叉构建只证明布局与打包流程，最终仍要在 Windows 宿主上验证。

## v0.17.18

0.17.17 的产物仍然约 90 MB，其中 site-packages 占 71.9 MB、PySide2 独占 67.5 MB。上一版只按 import 闭包删除模块、库和插件，留在包里的却是一整族用不上的东西：opengl32sw.dll（约 20 MB）、全套 Qt 翻译、另外两个操作系统的平台插件、未使用的图片格式、binding 的 docs/scripts/support、导入用 .lib，以及 shiboken2 只有构建工具才会加载的几个 MSVC 运行时。配置里只有 qt = "imports"，没有任何地方能表达“我这个桌面程序只要中文、只要 ico/gif/jpeg、只要桌面平台”，翻译过滤也只有一档粗糙的前缀匹配。

0.17.18 在 [tool.py_upper.optimize] 下补了四个可选开关：qt_software_opengl（bool，默认 true；关掉后删除 opengl32sw.dll）、qt_locales（"all" 或列表；精确 locale 只保留该变体，裸语言同时匹配地区变体，支持 glob）、qt_image_formats（"all" 或列表；PNG 编译在 QtGui 里，列表不含 svg 时还会顺带删掉 qsvgicon/qsvg 以及只被它们引用的 Qt5Svg/Qt6Svg）、qt_platforms（"all" 或列表，默认 windows/cocoa/xcb/wayland/offscreen；匹配前会去掉 q/lib 前缀和扩展名，所以同一份列表对 Windows、Linux、macOS 的 wheel 都成立；qwebgl 始终删除）。类型不对、空列表、含绝对路径或 .. 的取值会在删除任何文件之前报错。

只要启用 Qt 裁剪，binding 包里的 docs、scripts、support 和 *.lib 就无条件删除，shiboken 包再额外删掉同名目录/.lib 以及 vcamp140.dll、concrt140.dll、vcomp140.dll——这些都是构建期或文档文件，运行时不会 import。工作副本的 app/pyproject.toml 选了 qt_software_opengl = false、qt_locales = ["zh_CN"]、qt_image_formats = ["gif", "ico", "jpeg"]；模板仍保留全部负载，并用注释说明每个开关的取舍。README 与 docs/DEVELOPMENT_GUIDE.md 同步补充了开关、跨平台名称匹配和无条件删除项，版本号改为 0.17.18。

验证：python -m pytest app/tests -q 为 264 passed, 7 skipped（上一版 256 passed, 7 skipped），新增用例覆盖 docs/.lib/MSVC 删除、平台 allowlist、图片格式 allowlist 连带 SVG 引擎、locale 匹配（zh_CN / zh / de）、关闭软件 OpenGL 和非法取值；Windows x86_64 全量构建 [1/6]…[6/6] 通过，launcher 与目标 runtime 冒烟均过，日志为 Qt pruning: removed 432 file(s) and 32 director(ies), freed 299 MB，trim 再释放 66 个文件 / 2 个目录（4.9 MB）。dist/Upper 从 90.0 MB 降到 58.85 MB（-34.6%），site-packages 71.91 → 40.73 MB，PySide2 67.5 → 39.21 MB，shiboken2 4.22 → 1.34 MB，runtime 15.3 MB 未动；最终只留 Qt5Core/Gui/Widgets/Network/Qml、qwindows/qoffscreen、qico/qgif/qjpeg、qwindowsvistastyle、qtuiotouchplugin 和 qt_zh_CN.qm。另以 PY_UPPER_HEADLESS=1、QT_QPA_PLATFORM=offscreen 手动启动，退出码 0。限制：关掉软件 OpenGL 后，没有可用 GL 驱动的机器可能起不来；locale 和图片格式 allowlist 会删掉未列出的翻译/格式；runtime 仍有 15.3 MB，本轮未裁剪。

同一版本的后续提交把“通用裁剪”和“默认加密”变成默认行为：dist-info / egg-info、*.pyi、Qt 的 examples/include，以及 runtime 的 idlelib/test/include/pkgconfig 由 trim.py 的全局规则删除，不再要求每个项目在配置里重复；PySide6/shiboken6 的 doc/include/lib 和版本化的 tcl/tk 目录也会在 POSIX 上被裁剪。egg 在没有 [tool.py_upper.egg] 段时也默认开启，模板写 enabled = true，--no-egg 只对本次构建关闭，enabled = false 才会长期关闭。macOS arm64 实测产物 74 MB（site-packages 44 MB、runtime 29 MB），dist-info/egg-info/.pyi/examples/include 均为 0，codesign 与 headless 启动通过。

## v0.17.17

0.17.16 把加密归档直接放在可执行文件旁、命名 `app.egg`：名字既暴露了“这是 egg/zip”，又让包根目录多出一个和 runtime / site-packages 同级的大文件。0.17.17 新增 `EGG_DIR = "bundle"`，归档固定为 `bundle/py_upper.dat`——Windows/Linux 在可执行文件旁的 `bundle/`，macOS 在 `Contents/Resources/bundle/`；stage 也按同一布局落盘，launcher 的 `kEggDir` / `kEggFile` 负责解析。改目录或文件名只需要改这一处常量并重新构建。

裁剪侧新增 `preset = "release"`：在 safe 的显式规则之上删除 `**/*.dist-info` 与 `**/*.egg-info`。这些目录是 pip 和 `importlib.metadata` 描述“已安装分发”的元数据，冻结应用通常不读；删除后 `site-packages` 更干净，PySide2/shiboken2 两个 dist-info 约 0.6 MB。`minimal` 也包含这两条规则，`safe` 保持原样，不会静默改变别的项目。代价是运行时调用 `importlib.metadata.version()` 或 `pkg_resources.require()` 会失去数据，需要这类查询的应用应留在 safe，或用 `site_packages_keep` 保住对应的 dist-info。工作副本的 `app/pyproject.toml` 已切到 `release`，模板仍是 `safe` 并写明三档差异。

顺带修正了裁剪与 wheel manifest 的顺序：原先先裁剪、再扫描 `*.dist-info` 生成冒烟导入列表，`release` 一开就会得到空列表并让 `[5/6]` 失败；现在先记录第三方导入名，再做裁剪。

验证：`python -m pytest app/tests -q` 256 passed, 7 skipped（上一版 254），compileall 与 `git diff --check` 干净；Windows x86_64 全量构建 `[1/6]`…`[6/6]` 通过，产物里是 `bundle/py_upper.dat`、没有 `app.egg`，`site-packages` 不再有 `*.dist-info` / `*.egg-info`（比 0.17.16 少约 0.6 MB），launcher 与目标 runtime 冒烟均通过。

## v0.17.16

应用自己的 `.py` 和本次构建产出的扩展原先以散文件躺在 `site-packages` 里，打包后可以直接抄走，构建工具也没有开关能把它们收进一个封套。0.17.16 新增 `[tool.py_upper.egg]`：构建时把 `app/src` 里的应用模块和本次编译的扩展写进 `app.egg`（8 字节 MAGIC + SHAKE-256 密钥流异或的 zip），再从 staging 删掉原文件；launcher 在导入入口前安装内存导入器，所以包体里不再出现 `main.py` / `main.pyd`。

后端有三档：Windows 用 vendored `_memimporter` 直接把扩展从内存映射（`memory`），Linux/macOS 解密到私有临时目录再挂 `sys.path`（`extract`），`auto` 在 Windows 且 `_memimporter` 可用时走内存、否则回退临时目录。`memory` 需要 Zig 工具链；`auto` 编译失败会打印 warning 并继续，`memory` 则直接报错，不会静默产出一个跑不起来的包。归档支持 `deflated`/`stored`/`lzma`，条目排序且时间戳固定，同一输入可复现。launcher 内嵌 loader 源码、key 和 backend；stage 的 `egg.json` 记录本次实际使用的三项配置，`--verify` 与冒烟因此不会拿项目默认值去解一个被 CLI 覆盖过的 egg。`--egg` / `--no-egg`、`--egg-key`、`--egg-backend`、`--egg-compression` 可只对本次构建覆盖。

需要说清楚的是：这是提高逆向成本的混淆，不是强加密，密钥就编在 launcher 里；它也不会让产物明显变小——runtime、第三方 wheel 和 Qt 才是大头，裁剪仍然靠 `[tool.py_upper.trim]` 与 Qt prune。macOS 的 `extract` 后端会把未签名的 `.so` 写进临时目录，hardened runtime 下可能被 dyld 拒绝，需要 `com.apple.security.cs.disable-library-validation` 或改用已签名的独立原生库。

验证：`python -m pytest app/tests -q` 254 passed, 7 skipped（上一版 223），compileall 与 `git diff --check` 干净；Windows x86_64 全量构建 `[1/6]`…`[6/6]` 与 launcher/目标 runtime 冒烟全部通过，`dist/Upper/app.egg` 1,970,782 字节 / 24 个应用文件，`_memimporter.pyd` 198,144 字节，`site-packages/main.py` 与 `main.pyd` 均已消失。限制：`_memimporter` 仅 Windows；POSIX 未在这台 Windows 宿主上跑全量构建，归档、密钥、成员查找和三个后端由单元测试覆盖。

## v0.17.15

0.17.14 的 `minimal` 预设只按 Windows 的 PySide 平铺布局写：`*/plugins/...` 里的单个 `*` 不跨目录，于是它命中 `PySide2/plugins/...`，却漏掉 macOS/Linux 的 `PySide6/Qt/plugins/...`，也漏掉所有平台上的 `PyQt5/Qt5/...` 与 `PyQt6/Qt6/...`——预设在最需要它的平台上静默变成 no-op。同一类错误还在 Qt 自动裁剪里：`_qt_layout()` 只认 `Qt` 目录，PyQt 的 `Qt5`/`Qt6` 被当成 Windows 平铺布局，`plugins`、`qml`、`translations`、`metatypes` 全指向不存在的包根路径，`prune()` 于是什么也没删；`prune()` 自己又硬编码了一次 `pyside / "Qt"` 来决定 `examples`、`include`、`resources` 的位置。

修法是不再让规则依赖布局：minimal 改成 `PySide*/**/...` 与 `PyQt*/**/...`，`**/` 允许零层或多层目录，Windows 平铺、`PySide6/Qt`、`PyQt5/Qt5`、`PyQt6/Qt6` 四种布局共用同一组规则；`examples`、`include`、`opengl32sw.dll`、平台插件、翻译和 WebP/TIFF 插件都按这个前缀覆盖到四种 binding。模板里的 safe 默认规则也改成 `PySide*/**/examples` / `PySide*/**/include` / `PyQt*/**/...`，不再只匹配平铺布局。`_QtLayout` 新增 `root` 字段，`_qt_payload_root()` 依次探测 `Qt`、`Qt5`、`Qt6`，`prune()` 改用 `layout.root`，不再重新推导。测试新增四种布局的 trim 用例、四个 `_qt_layout` 参数化用例、平铺回退用例和一条 PyQt6 树的 `prune()` 用例。

验证：`python -m pytest app/tests -q` → 223 passed, 7 skipped（上一版 216 passed），compileall 与 `git diff --check` 干净；Windows x86_64 的 minimal 全量构建复跑仍为 64.6 MB / 221 文件。限制：macOS/Linux 的完整构建没有在这台 Windows 宿主上跑过，布局由单元测试覆盖，POSIX 宿主仍需各跑一次 `[1/6]`…`[6/6]` 确认。

## v0.17.14

0.17.13 的裁剪接口默认只做安全删除，最大的几块可选负载——20 MB 的 opengl32sw.dll、qdirect2d/qminimal 平台插件、Qt 翻译和 WebP/TIFF 图片插件——仍要每个项目自己写 glob。0.17.14 增加 `[tool.py_upper.trim].preset`：`safe`（默认）只跑显式配置的规则，`minimal` 在 safe 之外展开一组激进规则，构建时可用 `--trim-preset minimal` 临时覆盖。预设规则与显式规则合并后去重，keep 仍然优先；未知预设名在构建开始前报错。

用 minimal 重打 Windows x86_64（PySide2 5.15.2.1、CPython 3.8.10）：产物从 92.4 MB / 279 文件降到 **64.6 MB / 221 文件**，site-packages 77.4 MB → 48.4 MB，runtime 15.3 MB，launcher 0.92 MB；trim 从上一版的 45 个文件 / 4.2 MB 增至 103 个文件 / 33.4 MB（其中 opengl32sw.dll 约 20 MB）。产包里 opengl32sw.dll、qdirect2d.dll、qminimal.dll、qwebp.dll、qtiff.dll 都不在，qwindows.dll 与 qoffscreen.dll 保留，translations 只剩空目录，`[6/6]` 的 target runtime smoke 与 launcher smoke 都通过。代价：minimal 删掉软件 OpenGL 回退和可选平台/图片插件，VM、RDP 或无可用 GL 驱动的机器可能起不来，依赖 Qt 翻译、WebP/TIFF 的应用会缺功能；默认仍是 safe。测试 216 passed, 7 skipped（上一版 212 passed），compileall 与 git diff --check 干净。

## v0.17.13

打出来的 Qt 包仍然大，不是因为 Qt 剪得不够，而是因为剪裁只敢删 import 能证明的东西：C++ 头文件、示例树、`.pyi` 存根、runtime 里的 `tkinter`/`idlelib`/CPython tests 都没有 import 边，旧实现又没有配置面，只能整包带着；Qt 那边还漏了 `examples`、`include`、未使用 WebEngine 时的 `resources`，以及会牵连 QtQuick/QtQml 的 `qwebgl` 平台插件。

新增 `tools/py_upper/trim.py` 作为通用裁剪接口：`[tool.py_upper.trim]` 的 `site_packages` / `site_packages_keep` / `runtime` / `runtime_keep` 四组相对 glob，构建时分别作用于 staging 的 `site-packages` 与复制后的 runtime；`*`、`?`、`[]` 不跨目录，`**` 跨目录，命中目录删整棵子树，keep 优先并保护整个子树。删除父目录但子路径被 keep 时，实现用“强制裁剪前缀”继续向下走：父目录保留，其余子项照删，不会因为父目录被 keep 就整棵留下。绝对路径、盘符、`.`、`..`、空段和空模式在构建开始前报错，裁剪只改构建副本，不动 `runtimes/`、wheel 缓存和 `app/src`。单次构建用可重复的 `--trim-site`、`--trim-site-keep`、`--trim-runtime`、`--trim-runtime-keep` 追加，叠加在配置之上而不写回文件；日志分别打印两棵树释放的文件数、目录数和字节数。

模板默认带上一组安全规则：`**/*.pyi` 与 PySide2/PySide6 的 `examples`、`include`，runtime 的 `idlelib`、`tkinter`、`tcl`、`tests`、`test`；`opengl32sw.dll` 和 `qoffscreen` 仍由 Qt 裁剪的保留逻辑兜底。Qt 自动裁剪同时补上 `examples`/`include`、未使用 `QtWebEngineCore` 时的 `resources`，以及 `qwebgl` 插件（保留 `qoffscreen` 给 headless 冒烟）。

验证：`python -m pytest app/tests -q` → 212 passed, 7 skipped（0.17.12 是 192 passed, 7 skipped，本次新增 20 条）；`compileall` 与 `git diff --check` 干净。Windows x86_64 全量构建（PySide2 5.15.2.1、CPython 3.8.10）走完 `[1/6]`…`[6/6]` 并 `READY: dist/Upper`：产物从 120.2 MB / 791 文件降到 92.4 MB / 279 文件，`site-packages` 从 104.0 MB 降到 77.4 MB，runtime 15.3 MB，launcher 0.96 MB；Qt 裁剪删 364 个文件、29 个目录（压缩前 266 MB），trim 再删 45 个 `.pyi`（4.2 MB）。打包产物里 `PySide2/examples`、`PySide2/include`、`PySide2/resources` 与 `plugins/platforms/qwebgl.dll` 都不在，`qoffscreen.dll` 保留，launcher smoke 与 target runtime smoke 均通过。限制：Windows embeddable runtime 把 stdlib 压在 `python38.zip` 里，所以这次 runtime 那组规则没有命中、日志没有输出；POSIX 的 runtime 树是展开的目录，同一代码路径会在那里生效。

## v0.17.12

VS Code 的 launch.json 那条「打包应用」在 Windows 宿主上跑不完整：`tools/build.py --run` 停在 `[4/6] Package + native dependency closure`，报 `ERROR: PermissionError: [WinError 5] 拒绝访问。: '...\dist\Upper' -> '...\build\retired\Upper.37880'`，而在走到这一步之前，每个阶段都至少红过一次。三处根因叠在一起：Qt 剪裁的探针子进程拿着一个手搓的环境块（只有 `PYTHONPATH`、`PYTHONDONTWRITEBYTECODE` 和 POSIX 的 `PATH`），Windows 的 CPython 缺 `SystemRoot` 会在 preinit 就 abort（`_Py_HashRandomization_Init: failed to get random numbers to initialize Python`）；SDK 带的是官方 Windows 构建的 MSVC 版 `pyconfig.h`，`MS_WIN64`/`SIZEOF_VOID_P` 与 `snprintf → _snprintf` 这两处对 MinGW-w64 的 ABI 是错的；CMake 会把 `-DCMAKE_RC_COMPILER` 原样写进 `CMakeRCCompiler.cmake` 当字符串字面量重读，Windows 的反斜杠路径于是变成非法转义 `\X`，资源编译器还没被调用就死在 configure。修法：探针改成继承 `os.environ`、只覆盖它真正需要的那两个变量（与构建工具里其他 subprocess 一致）；新增 `toolchain.windows_gnu_cpython_defines(target)`（`-DHAVE_SNPRINTF` 恒有，`-DMS_WIN64` 只给 x86_64/arm64——32 位的 `pyconfig.h` 本来就走 `MS_WIN32` 分支），由 `ensure_zig()` 同时写进 `CC`/`CXX` 前端与 `Toolchain.compiler_args`，launcher 与扩展编译因此看同一份头文件视图，`python_build.compile_windows_extensions_with_zig()` 转发 `compiler_args`；`package._cmake_path()` 把四个 `-D` 路径值都按 `as_posix()` 拼（POSIX 上是恒等变换）。

打包闭包那两条是「轮子自己的毛病被当成 payload 的毛病」。win_amd64 的 PySide2/shiboken2 轮子里那个 32 位 `ucrtbase.dll` 没有任何镜像加载它、UCRT 也从 System32 取（它本来就在 `native/deps.py` 的宿主库白名单上），留在 payload 里只会让架构守卫否掉整包，所以按既有的轮子缺陷清单在 `[tool.py_upper.native].exclude` 里丢掉，同时让两条架构不匹配的报错带上「用 exclude 把这个外来文件丢掉」这条出路——不然读错误的人第一反应是放宽守卫。PySide 每个不 import 的工具都发两份（`lupdate.exe` 与 `pyside2-lupdate.exe`），带前缀那份链接的是工具自己要的 Qt 模块（`lupdate` 读 `Qt5Xml`）而不是应用 import 的那些，于是可达性剪裁把库删了、把引用它们的可执行文件留下了，闭包随之红——剪裁现在两种拼法一起丢。

`[6/6]` 的 `No module named 'QtCore'` 是因为信了 `top_level.txt`：它是「轮子的声明」而不是「装了什么的记录」，PySide2 与 shiboken2 都声明了一个谁也没装的裸 `QtCore`（模块其实在 `PySide2/`、`shiboken2/` 下），于是构建自己的冒烟程序去 import 一个不存在的模块。现在声明必须同时出现在 `RECORD` 里才算数，`RECORD` 缺席时才退回声明、再退回 dist 名。

`[4/6]` 退树那条：Windows 把「还有进程握着这棵树」和「你没权限」都报成裸的 access denied，调用点分辨不出，而持有者通常都很短命（上一轮 `--run` 还没退干净的应用、解析并执行过 `dist/**/python` 的编辑器或索引器、刚写完 payload 上的杀软扫描）。`fs.retire_tree()` 的三个步骤现在都走 `_housekeeping()`：只在 `winerror` 5/32/33（拒绝访问、共享冲突、锁冲突）上重试 30 次 × 0.25 秒，别的立刻抛出，等不到就在错误信息里点名可能的持有者；POSIX 的 `PermissionError` 没有 `winerror`，所以那边一次都不重试、行为与原来一致。

验证：`python -m pytest app/tests -q` → 192 passed, 7 skipped（0.17.11 是 183 passed, 5 skipped，本次新增 11 条，每条都先对着未修的源码验红）；`python tools/build.py --run` 在 Windows 宿主（PBS CPython 3.8.10 + PySide2 5.15.2.1）走完 `[1/6]`…`[6/6]`，打包应用启动、`Application exit code: 0`；`python tools/build.py doctor` 退出码 0；pyflakes、compileall 与 `git diff --check` 干净。限制：windows-x86（32 位）走 Zig 时「不该加 `-DMS_WIN64`」只是推理，CI 那条腿用的是 MSVC，这个组合没有真跑过；两条 ucrtbase 排除项是 Windows 专用的轮子缺陷，写在跨平台模板里对 Linux/macOS 不匹配任何文件；退树等待是 Windows 专属行为，POSIX 仍然第一次就失败；windows-x86 与 windows-arm64 上「双击能起」的最终确认仍是 CI 那三条 Windows 腿。

## v0.17.11

打包出来的 Windows 应用开不了窗，双击就弹崩溃框：`AttributeError: 'PySide2.QtWidgets.QApplication' object has no attribute 'exec'`，位置是 `mvvm/mvvm_bootstrap.py:65` 的 `MVVMQtApplication.run`。launcher 起来了、解释器起来了、Qt 也 import 了、`QApplication` 也建好了，然后进程在窗口出现之前就结束。根因是把事件循环入口的名字写死成 `self.qt_app.exec()`——那是 Qt6 的拼法，Qt5 的 wrapper 是在 `exec` 还是 Python 2 语句的年代生成的，PySide2/PyQt5 只发布 `exec_`；写死任一个就等于绑死一个 Qt 大版本，而且只有在真正进循环那一步才会炸。这条线一直没被跑到：构建的冒烟、单测、手工跑全走 headless 分支（只 `processEvents()`，不进循环），本机所有真 Qt 的腿装的都是 PySide6——恰好是 `exec` 拼对了的那个 binding，所以第一个走到交互路径的 Qt5 运行环境就是用户包出来的那个应用，一个词的差就进了 `dist/`。

修法跟 `Signal`/`Slot` 一样，是能力探测而不是版本表：`utils/qt_compat.py` 新增 `exec_loop(target, *args, **kwargs)`，按 `exec`→`exec_` 顺序探对象上有哪个就调哪个，参数和返回码原样透传，两个都没有时抛 `AttributeError: <type> has neither exec nor exec_ (binding X, Qt N)`；`mvvm_bootstrap.py` 改成 `exec_loop(self.qt_app)`，bootstrap 里再也不出现任何一个 Qt 大版本的名字。`app/tests/test_qt_compat.py` 除了三条分派/透传/报错用例，还加了一条 ast 不变式：`app/src` 里除 shim 外任何 `.exec(`/`.exec_(` 调用直接失败——跟"只有 shim 能出现 binding 名"同一个形状。`app/tests/test_mvvm_example.py` 把假应用拆成共享面 `FakeApplicationSurface`（`setAttribute` 做成 classmethod 并记进 `CREATED["qt_attributes"]`）+ Qt6 的 `FakeApplication`（`exec`）+ Qt5 的 `FakeQt5Application`（只有 `exec_`，而且构造函数断言 high-DPI 属性已在它之前设好），两条新测试分别用 PySide2 形状的假 binding 把整个 `run_gui()` 走通（改之前这里就是复现用户那个 AttributeError 的地方）和锁住 high-DPI 的设置顺序。

顺带修掉同一个函数里的第二个 Qt5 静默错误：`create_application()` 里那句 `app.setAttribute(Qt.AA_EnableHighDpiScaling)` 写在实例上、构造之后，而 Qt5 是在构造 `QApplication` 的时候读这些属性，晚设就是 Qt 打一行 `Attribute Qt::AA_EnableHighDpiScaling must be set before QCoreApplication is created` 然后忽略——设置看着在、warning 容易读过去、缩放从来没生效。现在改成构造前调类级静态方法 `QApplication.setAttribute(...)`。文档同步：开发指南 §9.1 加第 6 条（进循环只能走 `exec_loop`，ast 会拦）、§9.4 补说明、§13 排错表加一行把这个 AttributeError 指到 ≥0.17.11。

打包产物里那三个 `.int` 没了。之前 `<App>.int`（入口）、`_py_upper_static.int`（同一个入口的改名安全副本，逐字节相同）、`<App>.smoke.int`（构建验证用的冒烟程序）都是同一个程序的不同拷贝，其中 smoke 那份根本是构建数据、谁也不该装。根源是从 PyStand 继承的布局：入口是"可执行文件旁边的文件"，于是改名安全只能靠再写一份同样的文件，冒烟也复用了"写脚本让 launcher 跑"这套机制。现在打包时 `package.write_embedded_source()` 生成 `py_upper_embedded.cpp`（`py_upper_embedded::kEntry` 与 `kSmoke`，C++ raw string 字面量），`build_launcher` 用 `-DPY_UPPER_EMBEDDED_SOURCE=` 编进 launcher，CMake 顺带定义 `PY_UPPER_HAS_EMBEDDED_SOURCE`；`write_entry_scripts()`/`_entry_script()` 删掉，`package()` 一个 `.int` 都不写。`config.entry_source()` 成为入口程序的唯一来源（`package.py` 与 `verify.py` 共用，避免循环 import），Python 侧的 `STATIC_ENTRY` 撤掉，C++ 保留自己的同名常量给手工 bundle。解析顺序改成内嵌优先、文件链兜底：打包的 launcher 从不看自己旁边，老布局残留的 sidecar 不能 shadow 编译进去的入口；手工构建（没有生成源可内嵌）仍按 `<stem>.int` → `.py` → `.pyw` → `_py_upper_static.int` 找，`PY_UPPER_SMOKE=1` 走内嵌 smoke、手工构建回退 `<stem>.smoke.int`。`_embedded_literal()` 遇到破坏 raw string 分隔符或含三引号的 payload 直接抛错而不是悄悄换分隔符——后者会把 bootstrap 那层 Python 字面量提前结束，两者都是生成器不变式，错了就该响。`PY_UPPER_SCRIPT` 改成只在入口真是文件时导出，指向不存在的路径比不说更糟。

顺带修掉同一条线上的第二个缺陷：`py_quote()` 用 raw literal `r'...'` 却又把反斜杠转义成双写，raw 字符串原样保留，于是 Windows 路径到 Python 时每个分隔符都变成两个——函数里两套转义机制并存，只有一套该负责这件事。现在改成正常单引号字面量加真转义（反斜杠、单引号、`\n`、`\r`、`\t`、控制的 `\xNN`）。verify 也换了骨架：两条"文件存在"检查换成"入口目录里没有 `.int` sidecar"加"launcher 二进制里能读到 `entry_source()`"——后者才是文件存在性检查做不到的那件事，它证明编出来的二进制里装的就是配置里那个入口；`run_launcher_smoke()` 不再先找 `<stem>.smoke.int`，内嵌 smoke 由这次真实运行证明。

实测踩到一个只有真构建能发现的错：生成的符号声明原本写在 `PyUpper.cpp` 的匿名 namespace 里，那是另一个内部链接的符号，链接直接失败 `variable '(anonymous namespace)::py_upper_embedded::kEntry' has internal linkage but is not defined`，单测只读源码文本抓不到——把声明挪到文件作用域才对得上生成文件里的定义。

命名也收拾了一遍：应用名从占位的 `MyApp` 改成 `Upper`。真来源还是那三处——`app/pyproject.toml`（工作副本）与 `app/pyproject.toml.example`（入库模板）的 `[tool.py_upper.app].name`，加上两处互为镜像的兜底默认值（`app/src/utils/paths.py` 的 `_DEFAULT_APP_NAME`、`tools/py_upper/config.py` 的 `or "Upper"`），后两处回答的是同一个问题「既没有 launcher、也读不到 pyproject.toml 时叫什么」，只是分别站在打包边界的两侧，所以必须一起动；`pyproject.toml` 里那一行上方留了标注，写明它是唯一来源、改完要重新构建。顺带清掉「名字的第二份拷贝」：`app/tests/test_e2e_local.py` 的 `_packaged_root()` 原来手写 `dist/MyApp.app`，一改名就把这条本地端到端测试指到不存在的目录，现在改成读 fixture 自己的 `app/pyproject.toml`（走 `py_upper.compat.tomllib`）；`test_build_tool_compat.py` 那条包校验用例把 `verify.app_name` 钉在 fixture 目录实际用的名字上，跟同文件里 bundle 那几条一个做法。README「App 名称」重写成真能照着做的流程：改哪一行、入库模板要一起改、`name` 管到什么（bundle 名 / 可执行文件名 / `Info.plist` 名字字段 / Windows 版本资源 / 编译进 launcher 的 `PY_UPPER_APP_NAME` / 示例应用的窗口标题）、不管什么（可执行文件允许改名、`identifier`、项目名 `py_upper`），以及最容易踩的一条——改名后 `dist/` 里旧名字的 bundle 不会被自动删掉，只有 `--clean`（连同 `build/`）会清。开发指南的产物布局图、配置示例、裁剪体积实测、改名示例跟着换成 `Upper`，排错表加一行「改了 name 产物还是旧名」。

Verification: 183 tests pass（5 skipped，上一版 175 passed：Qt5 那条线新增 6 条——`exec_loop` 的 4 条 + 示例的 2 条，内嵌入口那条线删掉一条旧的 `.int` 改写检查、新增三条——生成 TU、`_embedded_literal` 拒绝 payload、`py_quote` 转义钉），矩阵那两条文件 77 passed, 2 skipped，`PY_UPPER_E2E=1` 的本地端到端 2 passed（就是那条原来自己写死名字的用例），pyflakes/compileall/git diff --check 干净；红绿对比真实——两条新示例测试在改前的源码上从 `mvvm_bootstrap.py:65` 复现出 `AttributeError: 'FakeQt5Application' object has no attribute 'exec'`，改后全绿；真 Qt 运行期两条腿都进了交互路径——PyQt5 5.15.11（arm64 wheel，装在 `/tmp/qt5venv`）报 `binding: PyQt5 qt_major: 5`、`run() exit code: 0` 且 high-DPI 那行 warning 消失，真 PySide6（staging site-packages + offscreen + `QTimer.singleShot(0, app.quit)`）`run()` 同样退出码 0。macOS arm64 全量构建 `[1/6]`…`[6/6]` 100 PASS / 0 FAIL、`--verify` 100 PASS / 0 FAIL、`READY: dist/Upper.app`、launcher 冒烟退出码 0、`find dist -name '*.int'` 为空、`Resources/` 只剩 `runtime` 与 `site-packages`、`PY_UPPER_SMOKE=1 …/Upper.app/Contents/MacOS/Upper` 打印 `SMOKE PASS launcher`、`codesign --verify --deep --strict` 通过、交互路径 6 秒后进程仍活（窗口建起来了）、`Info.plist` 的 `CFBundleName`/`CFBundleDisplayName`/`CFBundleExecutable` 都是 `Upper` 而 `CFBundleIdentifier` 不变、launcher 字节里 `Upper` 在而 `MyApp` 一个都没有、离屏探针在打包产物上打印出的真实窗口标题是 `Upper`（`PY_UPPER_APP_NAME=Whatever` 时是 `Whatever`）；`PY_UPPER_WINDOWS_TOOLCHAIN=zig` 交叉构建 windows-x86_64 是 106 PASS / 0 FAIL、`launcher subsystem=gui`、`READY: dist/Upper`、产物 0 个 `.int`，而且两段程序都能在 PE 字节里搜到（`from main import main`、`SMOKE PASS launcher`），生成的 `py_upper_resource.rc` 里 FileDescription/InternalName/ProductName 是 `Upper`、OriginalFilename 是 `Upper.exe`。行为实验都在 `/tmp` 的副本上跑、不动 dist：① 把可执行文件改名成 `Renamed` 再往 `Resources/` 放毒化的 `MyApp.int` 与 `_py_upper_static.int`（打印标记 + `SystemExit(99)`），应用照常起（`Hello from py_upper` / `GUI OK offscreen`，exit 0）、两个 sidecar 都没跑，一次同时证明内嵌优先和改名安全；② 不带 `PY_UPPER_EMBEDDED_SOURCE` 手工 cmake 出 85 KB launcher，配 `Manual.int` 时走文件链（marker 打印、`sys.argv[0]` 是 sidecar 路径、exit 7），没有 sidecar 时按文档报 `Entry script missing … Expected Manual.int, Manual.py, Manual.pyw, _py_upper_static.int or an entry compiled into the launcher`、exit 3；③ 把包里的 `main` 扩展删掉触发崩溃，traceback 最内帧是 `File "./MyApp.app/Contents/MacOS/MyApp", line 1, in <module>`，即 `ENTRY_FILE` 就是 launcher 自身、崩溃上报链路照旧；④ 往 site-packages 放 `sitecustomize.py` 打印环境，内嵌时 `PY_UPPER_SCRIPT=None`、`PY_UPPER_HOME`/`PY_UPPER_APP_NAME` 正常。改名后 `dist/MyApp.app` 与 `dist/MyApp` 原样留在那里（正是新排错项写的那件事），手工删掉了。限制有三条：Windows 包还是得在 Windows 上双击才算最终确认（交叉构建只证明 PE 里有入口字节、子系统是 GUI），而且本机跑不了 PySide2——没有 macOS arm64 wheel、Rosetta 也不可用（`arch -x86_64` 报 Bad CPU type），所以 Qt5 那条腿靠 PySide2 形状的假 binding 测试加 `exec_loop` 对真 PyQt5（它两个拼法都有）的探测撑着，最终确认还是得在踩到这个问题的 Windows 机器上重打一次：`python tools/build.py --target windows-x86_64`，然后双击 `dist\Upper\Upper.exe`；另外生成器不接受破坏 raw string 分隔符或含三引号的程序文本，今天入口三行、smoke 两行够用，真要携带这种文本该换成分隔符搜索或字节数组，而不是放宽这个检查；`PY_UPPER_SCRIPT` 在打包产物里不再导出属于行为变化，文档已同步。

## v0.17.9

Windows 目标以前只能在装了 Visual Studio 的 Windows 宿主上构建，macOS/Linux 宿主的 `resolve_toolchain` 在第一个编译步骤之前就报「Windows targets require a Windows/MSVC build host」——能不能构建目标和用哪个编译器被当成同一个决定。现在 `PY_UPPER_WINDOWS_TOOLCHAIN=zig` 打开 Zig 交叉路径：按宿主平台取原生 Zig（`PY_UPPER_ZIG_VERSION`，默认 0.16.0），下载后对 `ziglang.org/download/index.json` 里的 SHA256 校验，解到 `build/host-tools/zig/<version>-<host>` 并留 `.verified` 标记，编译时用 `zig cc/c++ -target <arch>-windows-gnu`（Zig 0.16 不带 MSVC 交叉 CRT，所以走 MinGW-w64 GNU ABI）；解包前逐条校验成员路径。`Toolchain` 新增 `compiler_args` 表示"把编译器路径变成可用前端要补的参数"，Windows 扩展因此不再依赖宿主 setuptools 驱动，launcher 的 CMake 拿到生成的 `zig-cc`/`zig-cxx`/`zig-rc` 包装脚本加 `CMAKE_SYSTEM_NAME=Windows`、`-municode`；交叉目标如实打印 `SKIP ... cross target` 而不是假装跑过冒烟。launcher 启动时把嵌入式 runtime 的 `python3XX._pth` 改名成 `.disabled`——那个文件会把 launcher 自己设的搜索路径里的 `site-packages` 切掉。macOS 的 `-arch` 从工具链环境里删掉：扩展自己传，launcher 用 `CMAKE_OSX_ARCHITECTURES`，不再有第二份架构真相。

Qt 侧，`utils/qt_compat.py` 从"函数表"改成模块代理：`from utils.qt_compat import QtWidgets` 拿到的是真的模块对象（PEP 562 属性解析），IDE 能补全 `QtWidgets.QMainWindow`、类型检查走 `TYPE_CHECKING` 里的 PySide6 stub，运行期导入这个模块不加载任何 binding；`Signal`/`Slot` 在两种 binding 下都能拼（`Signal`/`pyqtSignal`、`Slot`/`pyqtSlot`），拼错的 `Qt*` 子模块在 import 行就报错，测试锁住"只有这个文件能出现 binding 名"。`qt_prune` 改用 `ast` 读 import 而不是匹配一条正则，会识别实际使用的 binding（`pyside2`/`pyside6`/`pyqt5`/`pyqt6`）并跟进 `utils.qt_compat` 前缀，遇到解析不了的源文件直接抛错——旧实现匹配不到就打印「keeping the full Qt payload」继续把 1.2 GB 打包进去。MVVM 包落到 `app/src/mvvm/`，删掉引用了四个不存在模块的死 model，工厂各自只剩一个家（`mvvm/core/factory.py`），模板补上 Windows 形状的 PySide6 插件/QML exclude 前缀。

Verification: 175 tests pass (5 skipped，上一提交 141 passed，新增 30 条；全套在 CPython 3.8.20 上同样全绿，矩阵 3.8 腿跑的那两条文件 71 passed, 2 skipped；`PY_UPPER_E2E=1` 的 2 条本地端到端通过)，pyflakes/compileall/git diff --check 干净；macOS arm64 全量构建 `[1/6]`…`[6/6]` 100 PASS、剪枝释放 1101 MB（staging site-packages 1.2 GB → 109 MB，PySide6 1.2 GB → 105 MB；`dist/MyApp.app` 100 MB，其中 PySide6 58 MB），`--run` 退出码 0 且 `GUI OK offscreen`，签名校验通过；`PY_UPPER_WINDOWS_TOOLCHAIN=zig` 在 macOS arm64 上把 windows-x86_64 跑到 `READY: dist/MyApp`（433 个 native binary 校验）。Zig 产出的是 MinGW-w64 ABI，PE 包仍只能由 Windows CI 三条腿最终确认。子系统这条：三个交叉目标的 launcher 解析出来都是 `subsystem=2 (gui)`（windows-x86_64 / windows-arm64 / windows-x86），`PY_UPPER_LAUNCHER_CONSOLE=1` 的构建是 `subsystem=3 (console)` 且检查通过；改动前建出来的 dist launcher 在新检查下直接报 `launcher subsystem: console, expected gui`，用改后的源码重建则 PASS。windows-x86 的扩展经 `compile_windows_extensions_with_zig` 编出 `probe_module.cp313-win32.pyd`（`machine=0x14c`，导入 `python313.dll`）。

MVVM 这层补成能真正带起一个应用的骨架。`MVVMApplication` 自己持有 ViewModel/View 注册表（`register_viewmodel`/`get_viewmodel`、`register_view`/`get_view`、`register_service`/`resolve_service`）和 `start()`/`stop()`；`ServiceLocator` 改成实例级——类级注册表会让一个应用取到另一个应用的服务，正是容器要挡掉的那种隐式耦合；跟注册表重复的 `add_view()`/`run()` 删掉。`MVVMQtApplication.prepare()` 把顺序固定成 容器 → ViewModel → View → `start()`，子类只填 `initialize_viewmodels()`/`initialize_views()` 两个钩子。Qt 名字挪进真正建窗口的函数里（`create_application()`、`MainWindow.__init__`），`import application` 因此不加载任何 binding——pytest、pyflakes、Cython 编译都不需要装 Qt，这条有测试锁住。

绑定这层修了两个静默错误：绑定目标解析出来是 callable 就调用（`QLabel.setText`），旧实现 `setattr(label, "setText", value)` 只是把方法名覆盖成属性、界面不动；双向绑定必须显式给 `getter_name`，从 setter 反推 getter 在这里就是猜——Qt 的 `setText`/`text`、`setWindowTitle`/`windowTitle` 并不同形，猜错就是静默读错值，现在缺了直接抛 `ValueError`。

桌面上加了「关于 MVVM 架构」按钮当参考实现：按钮 → `MainViewModel.show_about_command` → `about_visible` 属性变化 → 主窗口据此开「关于」窗口，窗口里的「关闭」按钮 → `hide_about_command` → 属性回 False → 关闭。两个窗口共用一个 ViewModel，`about_visible` 是唯一真相源，窗口之间互不认识。

3.8 那条腿修掉了：矩阵里只有它红，3.9 到 3.13 全绿——那条腿跑 test_build_tool_compat.py 与 test_py_upper_config.py，会加载 app/tests/conftest.py，而新的 fake binding 辅助函数把参数注解写成 dict[str, dict[str, dict[str, object]]] 却没带 from __future__ import annotations；PEP 585 泛型在函数签名里是定义期求值，3.8 导入 conftest 时直接 TypeError: 'type' object is not subscriptable，一条测试都没跑到。缺的 future import 补上，并加了一条守卫测试：遍历 app/src、app/tests、tools，任何在定义期求值 PEP 585 泛型或 PEP 604 联合的模块都直接失败。

目录按架构收干净：`app/src/models/`（`AppState` 死代码）和 `app/src/plugins/`（空包，全仓库无人引用）删掉；service 层不删也不该删，它是 MVVM 的一部分，`app/src/services/hello.py` 迁进 `mvvm/services/greeting.py`，由容器注册后注入 ViewModel；`app/src/utils/` 保留——`utils.paths.app_name()` 是 launcher 契约。重名的那层也拆了：`app/src/core/` 跟框架自己的 `app/src/mvvm/core/` 撞名，组合根改成 `app/src/application.py`（唯一 import 四层的那个文件），Qt shim 搬进 launcher 契约所在的 `app/src/utils/qt_compat.py`，`utils/` 保持叶子——不 import 任何应用模块；`main.py` 仍是入口文件，只把控制权交给组合根。真 PySide6 6.11.0 + offscreen 点了一遍：主窗口 → 点按钮 → About 对话框出现 → 点关闭消失。

Zig 交叉这条路有两个缺陷，一个是编不出来，一个是编出来是错的。前者：`windows-x86` 在下载编译器之前就被拒掉，理由是「Zig 0.16 不带 32 位 Windows 交叉工具链」，实测不成立——同一个 Zig 用 `zig cc -target x86-windows-gnu` 能把真 launcher 和链接 `python313.lib` 的扩展都编成 PE32（`machine 0x14c`），而且打包三元组里的 `i686` 从来不是 Zig 认识的名字（它叫 `x86`），所以那条守卫连带着把它钉死的测试一起撤了，`CMAKE_SYSTEM_PROCESSOR` 改成按目标取（`x86`/`AMD64`/`ARM64`），不再是 arm64/AMD64 的三元判断——原来 32 位的 configure 会自报 AMD64。后者更严重：Zig 路径产出的是控制台子系统。CMake 用 `-mwindows` 表达 `WIN32_EXECUTABLE`，而 Zig 走的 clang 驱动只回一句 unused argument 就把它丢掉，于是交叉构建的 launcher 全是 `IMAGE_SUBSYSTEM_WINDOWS_CONSOLE`——从资源管理器双击会多弹一个控制台窗口，`AttachConsole` 那条路根本走不到；现在 `launcher/CMakeLists.txt` 在非 MSVC 链接器上直接要 `-Wl,--subsystem,windows`（MSVC 仍走 `WIN32_EXECUTABLE`，它的链接器不认 `-Wl,`），控制台逃生开关是另一个分支不受影响。为了让这个缺陷不能再静默通过，`BinaryInfo` 增加 `subsystem` 字段（读 `IMAGE_OPTIONAL_HEADER.Subsystem`，PE32/PE32+ 在这个偏移上一致），`verify.py` 对 Windows 包断言 launcher 的子系统——默认要 GUI，只有 `PY_UPPER_LAUNCHER_CONSOLE=1` 那种文档化的调试构建才允许 console。CI 三条 Windows 腿都是真 Windows + MSVC，Zig 这条路零覆盖，所以缺陷一直隐形。

## v0.17.8

Windows 构建在编译头一步就退出的问题修掉了：扩展链接要的导库被从发布 runtime 里找，而发布 runtime 自 0.17.7 起是 python.org 的嵌入 ZIP，里面根本没有 `libs/`——旧布局下它正好就是 PBS 安装树，正好带着 `libs/python3XX.lib`，所以这个假设一直隐形。`TargetPython` 里同时装着"编译要读的树"和"要发布的树"，前者叫 `sdk_root`：导库和头文件一样只从 SDK 取；两个 provider 也不再往同一个 key 里塞不同含义的目录（SDK 都叫 `sdk_root`，发布树叫 `runtime_root`）。用例改成把 `python313.lib` 只放在 runtime 树里，旧查找会静默通过、现在必须报错；本机端到端 fixture 也按同样的分工摆——编译要链接的导库放在 SDK 那一半，而不是被拷走的那一半。

Verification: 141 tests pass (5 skipped); a full macOS arm64 PySide6 build finished `[1/6]`…`[6/6]` with 40 PASS, launcher smoke, `--run` exit 0 and a valid code signature. Windows bundle 仍然只能由 Windows CI 那三条腿确认。

## v0.17.7

Windows packages now ship the official python.org embeddable runtime instead of the PBS `install_only_stripped` install tree. PBS still provides the full SDK for that same target Python — extension compilation, target wheel resolution and the Qt import-closure probe run its interpreter — but the SDK is a build input and never reaches `dist/`. The embeddable ZIP is cached per target, validated (flat members, no traversal, no duplicates, required files) before anything is written, unpacked unmodified into `runtimes/<target>/<python>`, and recorded in the lock file as `runtime.source = "python.org"` with its checksum; a lock from the old shape is rejected rather than silently rebuilt, and a pinned version python.org publishes no embeddable ZIP for fails with the version and target named. The launcher learned that an embeddable runtime keeps its standard extensions beside `python3XX.dll` rather than in `DLLs/`, and the package check now asserts that shape (`python3XX.zip`, `._pth`, no `Lib/`). The Windows runtime payload fell from 65 MB and 3 271 files to 20 MB and 36 files for 3.13.5 x86_64, with no pip left to prune; the tracked template moves to 3.13.5, the value that exists both as three Windows embeddable ZIPs and across all seven PBS SDKs.

Verification: 140 tests pass (5 skipped); all seven targets resolve PBS inputs at 3.13.5; the three real python.org ZIPs unpack with the expected flat layout and pass the architecture guard (0 failures over 89 images); windows-x86_64 ran the full SDK + manifest + lock path (`verify_lock` green); and a full macOS arm64 PySide6 build on 3.13.5 finished `[1/6]`…`[6/6]` with 40 PASS, launcher smoke, `--run` exit 0 and a valid code signature. The Windows bundle itself can only be exercised by the Windows CI legs.

## v0.17.6

Made application compilation incremental and parallel. Cython output and compiled extensions now live in `build/cache/`, keyed by content: a module whose source text, Cython version, target ABI, SDK and compiler flags are unchanged is taken from the cache instead of rebuilt, and only a module that changed is regenerated. The per-module `cython --force` pass over a freshly wiped tree and the one-blocking-subprocess-per-module loop are gone; invocations now run on a thread pool, one job per CPU, and `[tool.py_upper.build]` exposes `jobs` and `incremental`. On a synthetic 60-module application the Cython + extension stage fell from 48.0 s to 4.5 s cold, 0.14 s unchanged and 0.74 s after a single edit.

Verification: 115 tests pass (5 skipped), including 8 new ones; two consecutive full macOS arm64 PySide6 builds where the second reports every module up to date, with `--verify` green and the launcher smoke passing; and a 60-module scaling measurement.

## v0.17.5

Hardened the launcher, which until now was a thin PyStand port. Entry resolution gained a rename-safe `_py_upper_static.int` fallback, the bundle root is exported to the application instead of guessed, fatal errors surface through a native dialog and unhandled exceptions through a crash report the launcher reports after finalizing the interpreter, and the Windows build switched to a wide-character entry point, UTF-8 paths, a static CRT, the GUI subsystem with `AttachConsole`, and a generated version resource. The last PyStand leftover, the packaging-time `app/resources/config/app.toml` writer nothing read, is gone: the application name lives in `app/pyproject.toml` only, and `app/resources` is now an optional payload directory that is copied when present. That single name now reaches the application as well — the launcher compiles it in and exports `PY_UPPER_APP_NAME`, `utils.paths.app_name()` reads it and falls back to the same `pyproject.toml` in a source tree, so a renamed executable keeps its configured name instead of reporting `argv[0]`. The configuration itself moved to the usual template split: `app/pyproject.toml.example` is tracked, the working copy `app/pyproject.toml` is Git-ignored, and a missing working copy fails with the copy command instead of silently building the template.

The template itself pins nothing: `app/pyproject.toml.example` now declares `dependencies = []`, so cloning the repository no longer hands you someone else's version pins. The working copy keeps the real set, and the CI integration job materializes its own copy with the dependency set the repository's application needs, since that application has to import PySide6 to run.

Verification: full macOS arm64 PySide6 build with `--run` (exit 0, `GUI OK offscreen`), `--verify` 40 PASS / 0 FAIL, `codesign --verify --deep --strict` passes, a rename probe starts through the static entry and still reports `MyApp`, failure probes exit 1/3 with the expected messages, a deleted working copy stops `--doctor` and pytest with the copy command, the CI materialization script reproduces a working configuration from the empty template, and 70 tests pass (2 more with `PY_UPPER_E2E=1`).

## v0.17.4

Consolidated the build tool from 27 modules to 19 by merging the modules that wrapped the same concept (PBS resolution, target runtime, verification, packaging) and removing a dead facade plus four duplicate hash implementations. No behaviour change; the real PySide6 build, verification, signature check and window launch were re-run after the merge.

Verification: pyflakes clean, 62 tests passed with `PY_UPPER_E2E=1`, full macOS arm64 build with `--run`, `--verify` (39 PASS / 0 FAIL) and `codesign --verify --deep --strict`.

## v0.17.3

Fixed Qt pruning dropping the macOS cocoa platform plugin, which made packaged applications fail to start. Only real Qt modules now take part in the plugin decision, and the platform plugin category is never pruned.

Verification: the packaged launcher reports `PLATFORM cocoa` and opens a real window on macOS arm64, `--verify` and `codesign --verify --deep --strict` pass, and 60 tests pass.

## v0.17.2

The application now opens a real Qt window, and the packaged payload is pruned to the Qt modules the application imports. A PySide6 build on macOS arm64 went from 1.2 GB to 153 MB with the window still working and the bundle still passing signature verification.

Verification: 59 unit tests, the hermetic local end-to-end suite with `PY_UPPER_E2E=1`, and a full PySide6 6.11.0 build on macOS arm64 whose packaged window opens headlessly and whose `codesign --verify --deep --strict` check passes.

## v0.17.1

Reliability release for the first real third-party dependency set. macOS extension linking, packaging verification, ad-hoc signing order, wheel resolution semantics, pip transfer behaviour, native closure performance and the Windows module search path were all fixed, and the local end-to-end build is now a CI gate on every platform.

Verification: full suite with `PY_UPPER_E2E=1` on macOS arm64 (54 passed in 74s), plus a complete `PySide6==6.11.0` / `pyserial==3.5` / `bleak==3.0.2` / `qasync==0.28.0` build, package, smoke, launcher smoke, application run and `codesign --verify --deep --strict` check.

## v0.17.0

Structural Build Tool reorganization around a unified Target Contract. Third-party wheels are now first-class build inputs, target runtime import smoke is mandatory before `--run`, native application libraries participate in the same dependency closure, lock files preserve multiple targets, and `runtimes/` is a Git-ignored local cache.

Verification: 43 local unit/compatibility tests passed (1 skipped), plus a full Linux end-to-end build/run using a target runtime, Cythonized application, transitive third-party wheels, application-owned native libraries, launcher smoke, and separate lock/locked validation.

## v0.16.19

Problem:
- Automatic PBS resolution failed on HTTP 504 from the large paginated GitHub Releases endpoint.

Root cause:
- The resolver requested up to 100 releases with all of their assets before it knew which historical release contained the requested Python version.

Changes:
- Use Astral uv's generated exact-version runtime metadata as the first release lookup index.
- Fetch only the exact PBS release required for the requested Python/target pair.
- Add bounded retries/backoff for transient network failures and 504 gateway timeouts.
- Reuse the exact release response for runtime and SDK resolution within one build.

Verification:
- 47 tests passed locally.
- Linux doctor passed.
- git diff --check passed.

Impact:
- Automatic Python-version-only configuration no longer depends on the large paginated GitHub Releases response in the normal path.
## v0.16.18

**Problem solved:** py_upper could report an exact Python version as available in a PBS release while the runtime selector still rejected the corresponding asset.

**Root cause:** the diagnostic/release matcher and the runtime/SDK selectors were not using one shared asset predicate; the runtime and SDK selectors assumed a `cpython-X.Y.Z+...` prefix while the shared version parser also accepted a hyphen separator.

**Implementation:** introduced a single `matching_assets()` path used by runtime and SDK selection, release detection, and version diagnostics. Asset names and requested Python versions are normalized before matching. Added regression tests covering both PBS filename separators and selector/diagnostic consistency.

**Verification:** full local regression suite passed; `git diff --check` passed.

## v0.16.17

**Problem solved:** Projects had to manually coordinate a target Python patch version with a PBS release tag, and the runtime asset matcher rejected valid PBS filenames such as `cpython-3.11.10+20241016-aarch64-apple-darwin-install_only_stripped.tar.gz`.

**Root cause:** `[tool.py_upper.pbs].release` was effectively required, and `runtime.select_asset()` used a `cpython-{version}.` prefix instead of the actual `cpython-{version}+` PBS filename pattern.

**Implementation:** Made the PBS release pin optional. When omitted, py_upper searches PBS releases from newest to oldest and selects the newest release containing both the exact requested Python/target runtime and full SDK assets. Explicit release pins remain supported. Lock files record the resolved release. Added regression tests for exact asset matching, automatic release selection, and explicit pin behavior.

**Verification:** Full local test suite passed; `py_upper doctor` and `git diff --check` passed.

**Impact:** A project can now normally configure only one runtime version, for example `python = "3.11.10"`; py_upper handles the corresponding PBS release selection automatically while preserving reproducibility through lock files.

# v0.16.16

**Problem solved:** Selecting a target Python patch version that is absent from the configured PBS release produced an opaque asset-not-found error, and the local `runtimes/` directory was not explicitly protected from Git commits.

**Root cause:** PBS selectors only reported the missing requested asset; `.gitignore` had no `runtimes/` entry and globally ignored native binary extensions even though `app/src/` can legitimately contain prebuilt native inputs.

**Implementation:** Added detailed PBS runtime/SDK asset diagnostics that enumerate available Python versions for the requested target, best-effort search recent PBS releases for a suggested exact-match release, and preserve exact-version semantics. Added `runtimes/` to `.gitignore`, and narrowed native-binary ignore behavior so application-owned `.dylib`, `.so`, and `.pyd` files under `app/src/` can be tracked while generated outputs stay ignored through `build/` and `dist/`. Added regression tests.

**Verification:** Full local test suite passed; `git diff --check` passed.

# v0.16.15

### Problem
- The packaged macOS application loaded bundled CPython but failed to import compiled application extensions with `symbol not found in flat namespace '_PyArg_ValidateKeywordArguments'`.

### Root cause
- The launcher used `dlopen(..., RTLD_NOW | RTLD_LOCAL)` for `libpython`. Unix CPython extensions generally resolve Python C-API symbols from the global process namespace because they do not directly link against `libpython`.

### Changes
- Change bundled `libpython` loading to `RTLD_NOW | RTLD_GLOBAL` on Unix.
- Add a regression test for the loader flag.
- Bump the project version to 0.16.15.

### Verification
- Full local regression suite passed.
- Linux launcher build passed.
- `git diff --check` passed.

# v0.16.14

**Problem solved:** The packaged macOS/Linux application could fail with `ModuleNotFoundError: No module named 'main'` after Cythonized modules were placed in `site-packages`.

**Root cause:** Unix Cython extension files were renamed with a `cp313-...` suffix that the target CPython import machinery does not recognize.

**Implementation:** Added a target-aware extension suffix derived from the CPython naming convention (`.cpython-313-darwin.so`, `.cpython-313-x86_64-linux-gnu.so`, etc.) and reused it in packaging verification. Windows retains the `.cp313-...pyd` convention.

**Verification:** Full local regression suite passed; `git diff --check` passed.

# v0.16.13

**Problem solved:** Verification could fail with `FAIL core` and make a successful package return `SystemExit: 1` when the user's source tree did not contain a `core` package.

**Root cause:** `tools/py_upper/verify.py` assumed the sample project's `core/` directory was mandatory instead of validating the actual configured application layout.

**Implementation:** Replaced the hard-coded `core` staging check with a generic `site-packages` check. Real application modules continue to be validated from `selected_sources()`, so layouts such as `src/config` and `src/plugins` remain supported. Added a regression test covering a project without `core/`.

**Verification:** Full local test suite passed; `git diff --check` passed.

# v0.16.12

### Problem
- Applications with prebuilt native libraries under `app/src/` needed an explicit native-library packaging path, and macOS dylib identity paths were not normalized.
- A dylib's own `LC_ID_DYLIB` could also be mistaken for a dependency when parsing `otool -L` output.

### Root cause
- Source-tree copying was not explicitly native-aware and used a generic Python tree copier without regression coverage for `.dylib` payloads and symlinked native libraries.
- The Mach-O dependency parser treated the dylib's own install name as if it were another load dependency.
- macOS packaging rewrote dependency load commands but did not normalize bundled dylib `LC_ID_DYLIB` values.

### Changes
- Preserve native libraries and symlinks from `app/src/` into staged `site-packages` using the metadata-safe content copier.
- Keep native files in the same recursive native dependency graph as Cython extensions and bundled runtime libraries.
- Exclude Mach-O `LC_ID_DYLIB` from dependency edges returned by `otool -L`.
- Normalize bundled macOS dylib IDs to `@loader_path/<filename>`.
- Continue rewriting actual resolved dylib dependencies to `@loader_path`-relative paths.
- Document the supported `src` native-library workflow and add regression coverage.
- Bump the project version to 0.16.12.

### Verification
- Full local regression suite passed.
- `py_upper doctor --target linux-x86_64` passed.
- `git diff --check` passed.

# v0.16.11

### Problem
- macOS native dependency bundling could report valid PBS Tcl/Thread/Itcl libraries as unresolved.

### Root cause
- Native dependency resolution only checked the top level of each search root and did not emulate macOS `@rpath` entries. PBS places Tcl/Thread/Itcl dylibs below `runtime/lib`, so names such as `libtcl9thread3.0.6.dylib` were not found. The previous rewrite step also ignored dependencies that already used `@rpath`.

### Changes
- Recursively resolve native libraries by basename while preserving search-root priority.
- Parse Mach-O `LC_RPATH` entries and use them for `@rpath` resolution.
- Rewrite resolved macOS native dependencies to `@loader_path`-relative paths, including existing `@rpath` references.
- Copy externally resolved native dependencies by content only.
- Add regressions for nested Tcl-style dylibs and `@rpath` resolution.
- Bump the project version to 0.16.11.

### Verification
- Full local regression suite passed.
- `py_upper doctor --target linux-x86_64` passed.
- `git diff --check` passed.

# v0.16.10

### Problem
- macOS packaging could still fail while copying an ordinary read-only PBS runtime file, with `PermissionError: [Errno 1] Operation not permitted` from `os.chmod()`.

### Root cause
- The v0.16.9 custom copier stopped `copytree()`/`copy2()` metadata replay but still applied the full source POSIX mode to every copied file. A source mode such as `0444` caused the build to attempt a restrictive `chmod()` on the new file, which can be rejected by macOS in the app-bundle destination.

### Changes
- Make package-tree copies content-only; do not replay source POSIX modes or other filesystem metadata.
- Preserve executable status only for the launcher through an explicit platform packaging step.
- Keep runtime/native library copying independent of source executable/read-only modes.
- Add regressions for read-only runtime files, symlinks, and launcher executable mode.
- Bump the project version to 0.16.10.

### Verification
- Full local regression suite passed.
- `git diff --check` passed.

# v0.16.9

**Problem solved:** macOS app packaging failed during runtime tree copying with `Errno 1: Operation not permitted`.

**Root cause:** `shutil.copytree()` copied filesystem metadata/flags via its metadata-preserving copy path; macOS can reject reproducing those attributes in the destination app bundle.

**Implementation:** Added a packaging-specific recursive copier that transfers file contents, executable/readable mode, and symlink targets without cloning ACLs, xattrs, timestamps, or filesystem flags. Applied it to the runtime, site-packages, resources, and launcher.

**Verification:** 21 tests passed locally, including a regression test that makes `copystat()` fail with `EPERM`.

# v0.16.8

### Problem
- Every build from a development Python without Cython could trigger a `ModuleNotFoundError: No module named 'Cython'` under VS Code/debugpy before the bootstrap logic recovered.
- The v0.16.7 bootstrap path also used an unconditional `pip --upgrade`, which could reinstall Cython on every build instead of reusing the project cache.

### Root cause
- The Cython availability probe executed `import Cython` in a child Python process. A debugger can pause on that expected exception even though the parent process captures the non-zero exit status.
- Cache probing did not distinguish an already-populated supported Cython cache from a missing dependency before invoking pip.

### Changes
- Probe the installed Cython distribution through `importlib.metadata` without importing the `Cython` package.
- Reuse a py_upper-owned Cython cache when a supported 3.1.x distribution is already present.
- Remove the unconditional pip `--upgrade` flag from the bootstrap path.
- Keep the developer Python environment untouched.
- Add regression coverage for cache reuse and non-reinstall behavior.

### Verification
- Full local test suite passes.
- `git diff --check` passes.

# v0.16.7

**Problem solved:** The package staging logic left Python source files in the final application because only a subset of modules was Cythonized and `main.py` was explicitly excluded.

**Root cause:** `selected_sources()` limited compilation to `core.*`, `services.*`, and `models.*`, while `copy_python_tree()` copied all source files before deleting only the selected modules.

**Implementation:** Compile all non-`__init__.py` application modules by default, including the entry module and utility modules; keep only package marker `__init__.py` files as Python; move generated C output into `build/cython`; and filter development caches/bytecode from staging.

**Impact:** The packaged `Contents/Resources/site-packages` no longer contains application implementation `.py` files for the default configuration.

# v0.16.6

- Fixed Mach-O byte-order detection for macOS native dependency inspection. The previous parser reversed the arm64 CPU type and reported `0x0c000001`, rejecting valid Apple Silicon dylibs such as `libtcl9.0.dylib`. Universal Mach-O binaries are now checked for the requested architecture instead of being accepted unconditionally.

## 0.16.5

Fixes fresh-build failures caused by the selected development Python not having Cython installed. The build now bootstraps supported Cython 3.1.x into a py_upper-owned cache and uses it without modifying the developer's environment.

## 0.16.4

Fixes a macOS build failure where Cython exited successfully but the expected generated `app.c` file was not found. Cython is now invoked from each source directory using relative paths and `--force`, with clearer diagnostics when generation still does not produce the requested file.

## v0.16.3

### Problem
- The v0.16.2 PBS runtime resolver called `user_agent()` without importing it, so a normal PBS-backed build failed with `NameError` before downloading the runtime.

### Changes
- Import `user_agent` explicitly in `tools/py_upper/runtime.py`.
- Add a regression test covering the runtime module's User-Agent dependency.
- Keep the v0.16.2 Python 3.8+ compatibility changes unchanged.

### Verification
- 12 tests passed locally.
- `python tools/build.py --doctor --target linux-x86_64` passed.

# py_upper Git history

This repository preserves the complete release evolution from v0.9.1 through v0.16.16.

Every release has a dedicated commit and tag. The commit body records the problem, root cause, implementation changes, verification, and impact for that release. Earlier tags are immutable historical snapshots; v0.16.16 is the current PBS-version diagnostics and runtime-cache release.


## v0.16.2

**Problem solved:** The Build Tool failed on Python 3.10 because `tomllib` only exists in Python 3.11+. The build tooling also needed a clean Python 3.8 floor for future legacy-runtime work.

**Implementation:** Added a TOML compatibility layer with vendored Tomli for Python 3.8–3.10, kept standard-library `tomllib` for 3.11+, normalized build-tool annotation evaluation, added an explicit Build Tool Python minimum, and added compatibility tests/documentation.

**Impact:** Developers can use Python 3.8–3.13+ to run the Build Tool while independently selecting the bundled Target Python, including custom Python 3.8.10-compatible runtimes for legacy targets.
