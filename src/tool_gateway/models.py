from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PathInput(StrictModel):
    path: str = Field(default=".", min_length=1, max_length=512)


class EmptyInput(StrictModel):
    pass


class TicketInput(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    priority: Literal["low", "normal", "high"] = "normal"


class RepositoryOutput(StrictModel):
    path: str
    pyproject_present: bool
    tests_present: bool
    entries: list[str]


class FileOutput(StrictModel):
    path: str
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class StatusOutput(StrictModel):
    status: Literal["ok"]


class TicketOutput(TicketInput):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


class Registration(StrictModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    adapter: Literal["repository.inspect", "file.metadata", "api.status", "api.ticket"]


class Invocation(StrictModel):
    arguments: dict[str, Any]
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+$")
    approval_id: str | None = Field(default=None, max_length=64)


class ApprovalRequest(StrictModel):
    tool_id: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any]


class Decision(StrictModel):
    approve: bool


class GatewayError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)
