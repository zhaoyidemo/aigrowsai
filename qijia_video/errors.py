"""领域错误。API 适配器负责把它们映射为 HTTP 状态。"""


class QijiaVideoError(RuntimeError):
    status_code = 400


class ResourceNotFound(QijiaVideoError):
    status_code = 404


class AccessDenied(QijiaVideoError):
    status_code = 403


class RevisionConflict(QijiaVideoError):
    status_code = 409


class InvalidTransition(QijiaVideoError):
    status_code = 409


class ProviderUnavailable(QijiaVideoError):
    status_code = 503


class ResearchEvidenceUnavailable(ProviderUnavailable):
    """Provider failure carrying bounded, non-sensitive citation diagnostics."""

    def __init__(self, message: str, diagnostics: dict | None = None):
        super().__init__(message)
        self.diagnostics = dict(diagnostics or {})


class QualityGateFailed(QijiaVideoError):
    status_code = 422
