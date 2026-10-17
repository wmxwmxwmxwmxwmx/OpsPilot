from typing import Literal

from app.core.logging import log_event

SessionErrorCode = Literal[
    "session_disabled", "session_not_found", "session_conflict",
    "session_storage_error", "session_data_error", "session_too_large",
]

_ERRORS: dict[str, tuple[int, str]] = {
    "session_disabled": (503, "会话功能未启用。"),
    "session_not_found": (404, "会话不存在或已过期，请创建新会话。"),
    "session_conflict": (409, "会话已被其他请求更新，请基于最新上下文重试。"),
    "session_storage_error": (503, "会话存储暂时不可用，提交结果可能未知。"),
    "session_data_error": (503, "会话数据无效，无法继续诊断。"),
    "session_too_large": (413, "会话消息或整体大小超过限制。"),
}


class SessionError(Exception):
    def __init__(self, code: SessionErrorCode) -> None:
        self.code = code
        self.status_code, self.message = _ERRORS[code]
        super().__init__(self.message)
        # Fixed metadata only: never log keys, URLs, messages or exception text.
        log_event("session_failure", code=code)
