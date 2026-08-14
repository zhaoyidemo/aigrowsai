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


class ProviderRequestNotSubmitted(ProviderUnavailable):
    """The provider never received the paid request, so retrying is safe."""


class ProviderSubmissionUnknown(ProviderUnavailable):
    """A paid request may have been accepted, but no result was received."""


class UsageLedgerUnavailable(ProviderUnavailable):
    """A paid provider call happened but its usage record could not persist."""


class QualityGateFailed(QijiaVideoError):
    status_code = 422
