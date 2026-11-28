# 中文维护说明：启动示例应用；业务装配位于 application，避免入口复制生命周期逻辑。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
from application import main

if __name__ == "__main__":
    raise SystemExit(main())
