from typing import Literal
from uuid import UUID

IncidentErrorCode = Literal[
    "incident_storage_error", "incident_commit_unknown", "incident_data_error",
    "incident_too_large", "incident_partial_failure", "incident_partial_unknown",
]

_MESSAGES = {
    "incident_storage_error": "诊断记录存储暂时不可用，本次未提交诊断记录。",
    "incident_commit_unknown": "诊断记录提交结果未知，请勿自动重跑诊断。",
    "incident_data_error": "诊断记录数据无效。",
    "incident_too_large": "诊断记录大小超过限制。",
    "incident_partial_failure": "会话已保存，但诊断记录未提交；请勿自动重跑诊断。",
    "incident_partial_unknown": "会话已保存，诊断记录提交结果未知；请勿自动重跑诊断。",
}


class IncidentError(Exception):
    """Safe domain failure; HTTP mapping belongs to the API boundary."""

    def __init__(self, code: IncidentErrorCode, *, incident_id: UUID | None = None,
                 session_committed: bool = False) -> None:
        self.code = code
        self.message = _MESSAGES[code]
        self.incident_id = incident_id
        self.session_committed = session_committed
        super().__init__(self.message)

    def after_session_commit(self) -> "IncidentError":
        code = ("incident_partial_unknown" if self.code == "incident_commit_unknown"
                else "incident_partial_failure")
        return IncidentError(code, incident_id=self.incident_id, session_committed=True)
