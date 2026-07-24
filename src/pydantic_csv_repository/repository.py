"""Storage-independent repository interface and errors."""

from __future__ import annotations

from typing import Generic, Protocol, TypeVar


RecordT = TypeVar("RecordT")
RecordIdT = TypeVar("RecordIdT")


class RepositoryError(RuntimeError):
    """Repository access or persistence failed."""


class RecordNotFoundError(RepositoryError):
    """The requested record does not exist."""


class RecordConflictError(RepositoryError):
    """The record violates an identity or business uniqueness constraint."""


class DataValidationError(RepositoryError):
    """The current or candidate data failed storage or domain validation."""


class CommitDurabilityError(RepositoryError):
    """The replacement completed, but durable directory sync failed."""


class Repository(Protocol, Generic[RecordT, RecordIdT]):
    """Minimal CRUD interface for one record type."""

    def all(self) -> list[RecordT]: ...

    def get(self, record_id: RecordIdT) -> RecordT | None: ...

    def create(self, record: RecordT) -> RecordT: ...

    def update(self, record_id: RecordIdT, record: RecordT) -> RecordT: ...

    def delete(self, record_id: RecordIdT) -> bool: ...
