# orchestrator/api/models.py
# Pydantic v2 request/response models for the REST API (API-01 through API-06).
from pydantic import BaseModel, Field


class GoalRequest(BaseModel):
    """Request body for POST /goals.

    min_length=1 ensures empty-string goals are rejected with HTTP 422 before
    the job_id is generated or any DB write occurs.
    """

    goal: str = Field(..., min_length=1)


class JobStatusResponse(BaseModel):
    """Response body for GET /jobs/{job_id}/status."""

    job_id: str
    status: str


class JobResultResponse(BaseModel):
    """Response body for GET /jobs/{job_id}/result.

    result is None until the job reaches DONE status.
    """

    job_id: str
    result: str | None
