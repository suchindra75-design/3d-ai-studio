from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Schema(BaseModel):
    """Base for API schemas. Fields with defaults are still always present in
    responses, so the generated client types mark them as required."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class APIModel(Schema):
    model_config = ConfigDict(
        from_attributes=True, json_schema_serialization_defaults_required=True
    )


class ErrorBody(Schema):
    code: str = Field(examples=["not_found"])
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class ErrorResponse(Schema):
    error: ErrorBody


class PageMeta(Schema):
    total: int
    limit: int
    offset: int
