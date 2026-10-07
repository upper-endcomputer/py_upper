#pragma once
#include <string>
#include <vector>

class PyUpper {
public:
    // argv[0] is the launcher path; the remaining entries are passed through to
    // the application. The path arrives as UTF-8 on every platform, including
    // Windows, where main.cpp converts the wide command line.
    static int run(const std::vector<std::string>& argv);
};
