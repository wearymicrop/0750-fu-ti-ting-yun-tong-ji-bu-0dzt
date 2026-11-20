class BusinessError(Exception):
    def __init__(self, code, message, status=422, details=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details
