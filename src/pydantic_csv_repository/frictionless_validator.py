"""Frictionless Resource validation adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from frictionless import Resource, Schema

from pydantic_csv_repository.repository import DataValidationError


class FrictionlessResourceValidator:
    """Validate candidate CSV files against a fixed Frictionless schema."""

    def __init__(self, schema_descriptor: dict[str, Any]) -> None:
        self.schema = Schema(schema_descriptor)

    def __call__(self, candidate_path: Path) -> None:
        """Validate a candidate file and raise a concise error summary."""
        resource = Resource(
            path=candidate_path.name,
            basepath=str(candidate_path.parent),
            schema=self.schema,
        )
        report = resource.validate(limit_errors=20)
        if report.valid:
            return
        errors = report.flatten(["rowNumber", "fieldName", "type", "note"])
        raise DataValidationError(f"Frictionless validation failed: {errors[:20]}")
