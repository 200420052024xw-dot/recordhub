"""对话入口（WorkBuddy）批改口径：取待批改清单、逐条收下老师的批改结果。

刻意不在这里导入子模块：调用方从 ``service.workbuddy_review.service`` 取服务、
从 ``service.workbuddy_review.models`` 取请求模型，避免导入本包时牵出重依赖。
"""
