class WorkflowError(Exception):
    """Only a server-defined code can enter checkpoint errors or HTTP responses."""

    def __init__(self, code: str, *, status_code: int = 409, resumable: bool = False):
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.resumable = resumable


class TransientWorkflowError(WorkflowError):
    def __init__(self, code: str):
        super().__init__(code, status_code=503, resumable=True)
