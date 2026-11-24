class ChainError(RuntimeError):

    def __init__(self, message: str = "", reason: str | None = None):
        super().__init__(message)
        self.reason = reason
        self.tx_hash: str | None = None


class StaleParentError(ChainError):

    def __init__(self, message: str, head: str):
        super().__init__(message, reason="StaleParent")
        self.head = head
