/**
 * @file PyUpper.h
 * @brief 声明 launcher 统一入口；参数采用 UTF-8，返回值传递应用或启动错误状态。
 * @note 中文说明为本项目补充；原版权、许可和上游注释保留。
 */
#pragma once
#include <string>
#include <vector>

class PyUpper {
public:
    // argv[0] is the launcher path; the remaining entries are passed through to
    // the application. The path arrives as UTF-8 on every platform, including
    // Windows, where main.cpp converts the wide command line.
    /** @brief 以 UTF-8 参数启动内嵌应用。
     * @param argv 首项为 launcher 路径，其余参数传给应用。
     * @return 应用退出码或启动失败码；调用者负责作为进程状态返回。
     */
    static int run(const std::vector<std::string>& argv);
};
