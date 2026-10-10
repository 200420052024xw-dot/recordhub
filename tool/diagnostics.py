"""Safe, useful diagnostics shared by logs, persisted errors and alerts."""

import json
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import signature

_context = ContextVar("diagnostic_context", default={})


EVENT_EXPLANATIONS = {
    "admin_alert_delivery_failed": "管理员错误告警发送失败，请检查飞书连接、应用权限和管理员 OpenID。",
    "admin_alert_queued": "错误告警已进入发送队列。",
    "admin_alert_sent": "错误告警已成功发送给管理员。",
    "admin_alerts_disabled": "管理员错误告警未启用，因为没有配置管理员 OpenID。",
    "admin_alerts_enabled": "管理员错误告警已经启用。",
    "admin_notification_send_failed": "管理员按日期发送消息时发生错误。",
    "admin_notification_send_finished": "管理员按日期发送消息的请求已处理完毕。",
    "admin_notification_send_requested": "管理员提交了按日期发送消息的请求。",
    "auto_advance_result": "人工评价截止后的自动收尾处理已结束，请查看结果字段确认业务是否成功。",
    "command_failed": "命令行任务执行失败。",
    "confirmation_received": "已收到并处理人工评价确认。",
    "confirmation_rejected": "人工评价确认未被接受，请查看异常详情。",
    "feishu_event_callback_failed": "飞书事件回调解析失败。",
    "feishu_event_enqueued": "飞书数据变更事件已进入处理队列。",
    "feishu_event_stream_connecting": "正在连接飞书事件长连接。",
    "feishu_event_stream_failed": "飞书事件长连接运行失败。",
    "feishu_event_stream_loop_exited": "飞书事件长连接已意外退出，重启服务前无法实时接收确认事件。",
    "feishu_event_stream_started": "飞书事件监听服务已启动。",
    "feishu_event_worker_failed": "飞书事件后台处理失败。",
    "feishu_request_completed": "飞书开放接口请求成功。",
    "feishu_request_failed": "飞书开放接口请求失败，请查看接口路径、错误码和请求编号。",
    "http_request_failed": "HTTP 接口处理发生未预期错误。",
    "llm_attempt_failed": "本次 AI 调用或结果校验失败，程序将按配置决定是否重试。",
    "llm_attempts_exhausted": "AI 调用已达到最大尝试次数，本次任务无法继续生成结果。",
    "manual_notification_missing_recipient": "管理员手动发送时找不到有效收件人 OpenID。",
    "message_send_attempt": "正在调用飞书消息接口发送消息。",
    "message_send_failed": "飞书消息发送失败。",
    "message_sent": "飞书消息发送成功。",
    "notification_attempt": "业务消息已登记发送尝试。",
    "notification_capture_failed": "启动时保存历史待发消息失败。",
    "notification_capture_not_ready": "该日期尚未准备好可保存的待发消息。",
    "notification_delivered": "业务消息已成功送达并记录发送凭据。",
    "notification_failed": "业务消息发送失败，后续不会自动补发。",
    "notification_replay_failed": "管理员手动重发历史消息失败。",
    "notification_suppressed": "该消息属于历史或已处理消息，已停止自动发送。",
    "organization_cache_loaded": "组织人员和部门缓存已加载。",
    "organization_cache_refreshed": "组织人员和部门缓存已刷新。",
    "organization_refresh_failed": "定时刷新组织信息失败，当前继续使用已有缓存。",
    "organization_refresh_failed_using_persisted_cache": "启动刷新组织信息失败，已改用本地持久化缓存。",
    "scheduled_workflow1_run": "每日工作日志流程已按计划启动。",
    "service_started": "RecordHub 服务已成功启动。",
    "service_stopped": "RecordHub 服务已经停止。",
    "service_stopping": "RecordHub 服务正在关闭。",
    "serving": "RecordHub HTTP 服务正在启动。",
    "snapshot_cleanup_removed": "过期且已完成的本地流程状态已清理。",
    "workflow1_already_completed": "该日期的每日流程已经完成，本次不重复执行。",
    "workflow1_failed": "每日工作日志流程执行失败。",
    "workflow1_finalize_failed": "每日流程在截止收尾阶段失败，请查看失败阶段和异常详情。",
    "workflow1_finalize_not_ready": "每日流程尚未满足截止收尾条件，未执行收尾。",
    "workflow1_finalized": "每日流程的人工评价截止收尾已成功完成。",
    "workflow1_finished": "每日工作日志流程本次执行结束，请根据状态字段判断最终状态。",
    "workflow1_issue_recorded": "每日流程发现业务异常，已写入当日异常台账。",
    "workflow1_log_failed": "单条工作日志的 AI 评价生成失败。",
    "workflow1_logs_evaluated": "本批工作日志的 AI 评价处理结束。",
    "workflow1_notification_failed": "每日流程消息发送失败，其他收件人仍会继续处理。",
    "workflow1_resume": "管理员正在恢复并重试指定日期的每日流程。",
    "workflow1_start": "每日工作日志流程开始处理指定日期。",
    "workflow2_activated": "周期分析流程已启用，并记录首次启用日期。",
    "workflow2_admin_resume_failed": "管理员恢复周期任务失败。",
    "workflow2_admin_start_failed": "管理员启动周期任务失败。",
    "workflow2_already_completed": "该周期任务已经完成，本次不重复执行。",
    "workflow2_blocked": "周期任务因数据或配置问题被阻塞。",
    "workflow2_completed": "周期分析任务已成功完成。",
    "workflow2_confirmation_event": "已收到周期分析确认事件并检查待处理任务。",
    "workflow2_failed": "周期分析任务执行失败。",
    "workflow2_not_due": "今天不是该周期任务的计划运行日期。",
    "workflow2_notification_failed": "周期分析消息发送失败，后续不会自动补发。",
    "workflow2_notification_sent": "周期分析消息发送成功。",
    "workflow2_recover": "周期任务恢复检查已经完成。",
    "workflow2_resume": "正在恢复指定的周期任务。",
    "workflow2_start": "周期分析任务开始执行。",
    "workflow2_waiting_confirmation": "周期分析已生成草稿，正在等待人工确认。",
}

LOGGER_EXPLANATIONS = {
    "apscheduler.executors": "定时任务执行状态。",
    "apscheduler.scheduler": "定时调度器运行状态。",
    "uvicorn.access": "HTTP 请求访问记录。",
    "uvicorn.error": "HTTP 服务运行状态。",
    "Lark": "飞书事件长连接运行状态。",
}

CONTEXT_LABELS = {
    "workflow": "工作流",
    "target_date": "业务日期",
    "run_id": "任务编号",
    "operation": "处理操作",
    "log_id": "日志编号",
    "person_id": "人员编号",
    "notification_key": "消息业务键",
    "recipient": "收件人OpenID",
}

EXCEPTION_EXPLANATIONS = {
    "AssertionError": "程序内部条件检查失败。",
    "FeishuApiError": "飞书开放接口返回错误。",
    "TransportError": "网络连接、超时或远程传输失败。",
    "DeepSeekApiError": "AI 服务接口返回错误。",
    "StructuredOutputError": "AI 返回内容不符合要求的结构。",
    "LLMError": "AI 调用多次尝试后仍然失败。",
    "LLMValidationError": "AI 返回内容校验失败。",
    "ValueError": "输入数据、配置或业务状态不符合要求。",
    "WorkflowStateError": "本地工作流状态文件缺失、损坏或版本不兼容。",
}


@contextmanager
def diagnostic_context(**values):
    token = _context.set({**_context.get(), **values})
    try:
        yield
    finally:
        _context.reset(token)


def workflow_diagnostics(workflow: str):
    def decorate(function):
        target_name = list(signature(function).parameters)[1]
        @wraps(function)
        def call(self, *args, **kwargs):
            target = args[0] if args else kwargs[target_name]
            with diagnostic_context(workflow=workflow,
                                    target_date=str(getattr(target, "scheduled_date", target)),
                                    run_id=getattr(target, "run_id", "-"),
                                    operation=function.__name__):
                return function(self, *args, **kwargs)
        return call
    return decorate


def redact(value: str) -> str:
    value = re.sub(r"(?i)(Bearer\s+)[^\s\"']+", r"\1[REDACTED]", value)
    return re.sub(
        r"(?i)((?:access_key|ticket|app_secret|api_key|tenant_access_token|"
        r"access_token|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s&\"',}]+",
        r"\1[REDACTED]", value)


def error_summary(exc: BaseException) -> str:
    parts = [f"{type(exc).__name__}: {str(exc) or '(no message)'}"]
    for name in ("provider", "status_code", "code", "method", "path", "request_id"):
        value = getattr(exc, name, None)
        if value is not None:
            parts.append(f"{name}={value}")
    details = getattr(exc, "details", None)
    if details is not None:
        parts.append("response=" + json.dumps(details, ensure_ascii=False, default=str))
    return redact(" ".join(parts))


class DiagnosticFormatter(logging.Formatter):
    @staticmethod
    def _explanation(record: logging.LogRecord) -> str:
        message = record.getMessage().strip()
        event = message.split(maxsplit=1)[0] if message else ""
        if event in EVENT_EXPLANATIONS:
            return EVENT_EXPLANATIONS[event]
        for logger_prefix, explanation in LOGGER_EXPLANATIONS.items():
            if record.name == logger_prefix or record.name.startswith(logger_prefix + "."):
                return explanation
        if record.levelno >= logging.ERROR:
            return "程序发生错误，请结合异常详情、业务上下文和前后日志定位原因。"
        if record.levelno >= logging.WARNING:
            return "程序发出警告，请检查相关状态是否符合预期。"
        return ""

    def format(self, record: logging.LogRecord) -> str:
        output = super().format(record)
        explanation = self._explanation(record)
        if explanation:
            output += "\n中文说明=" + explanation
        if _context.get():
            translated = {CONTEXT_LABELS.get(key, key): value
                          for key, value in _context.get().items()}
            output += "\n业务上下文=" + json.dumps(translated, ensure_ascii=False, default=str)
        if record.exc_info and record.exc_info[1]:
            exc = record.exc_info[1]
            output += "\n异常详情=" + error_summary(exc)
            explanation = EXCEPTION_EXPLANATIONS.get(type(exc).__name__)
            if explanation:
                output += "\n错误类型说明=" + explanation
        return redact(output)
