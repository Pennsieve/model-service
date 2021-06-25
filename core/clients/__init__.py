from typing import Dict

from audit_middleware import TraceId  # type: ignore
from flask import current_app, has_request_context

from .audit_logger import AuditLogger, Auditor  # noqa: F401
from .header import trace_id_header, with_trace_id_header  # noqa: F401
from .pennsieve_api import PennsieveApiClient  # noqa: F401
from .pennsieve_jobs import (
    CreateModel,
    CreateModelProperty,
    CreateRecord,
    DeleteModel,
    DeleteModelProperty,
    DeleteRecord,
    PennsieveJobsClient,
    UpdateModel,
    UpdateModelProperty,
    UpdateRecord,
)
from .victor_ops import VictorOpsClient  # noqa: F401
