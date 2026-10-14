"""对话入口（WorkBuddy）查询口径：认人与按权限查日志 / 评价 / 阶段工作。

刻意不在这里导入子模块：调用方从 ``service.workbuddy_query.service`` 取服务、
从 ``service.workbuddy_query.models`` 取请求模型，避免导入本包时牵出重依赖。
"""
