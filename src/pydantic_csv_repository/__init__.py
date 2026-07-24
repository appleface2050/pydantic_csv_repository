"""Safe local CSV persistence for Pydantic models."""

from pydantic_csv_repository.csv_repository import CsvRepository, FieldCodec
from pydantic_csv_repository.repository import (
    CommitDurabilityError,
    DataValidationError,
    RecordConflictError,
    RecordNotFoundError,
    Repository,
    RepositoryError,
)

__all__ = [
    "CsvRepository",
    "FieldCodec",
    "CommitDurabilityError",
    "DataValidationError",
    "RecordConflictError",
    "RecordNotFoundError",
    "Repository",
    "RepositoryError",
]


def __getattr__(name: str):
    """Load optional integrations only when they are requested."""
    if name == "FrictionlessResourceValidator":
        from pydantic_csv_repository.frictionless_validator import FrictionlessResourceValidator

        return FrictionlessResourceValidator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
