"""Request errors with an error code instead of prose (ADR 0006)."""

from rest_framework import status
from rest_framework.exceptions import APIException


class CodedError(APIException):
    """Answers ``{"code": code, "details": details}`` with ``status_code``."""

    def __init__(self, code: str, status_code: int = status.HTTP_400_BAD_REQUEST, details=None):
        self.status_code = status_code
        super().__init__({'code': code, 'details': details or {}})
