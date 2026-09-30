#include "PyUpper.h"
#include <string>
#include <vector>
int main(int argc,char**argv){if(argc<1)return 1;std::vector<std::string>a;for(int i=1;i<argc;++i)a.emplace_back(argv[i]);return PyUpper().run(argv[0],a);}
