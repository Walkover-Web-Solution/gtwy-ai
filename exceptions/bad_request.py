class BadRequestException(Exception):
    def __init__(self, message="Bad request"):
        super().__init__(message)
