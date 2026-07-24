"""Safe CSV repository implementation backed by Pydantic models."""

from __future__ import annotations

import csv
import datetime
import fcntl
import os
import shutil
import tempfile
from collections.abc import Callable, Iterable, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from pydantic_csv_repository.repository import (
    DataValidationError,
    RecordConflictError,
    RecordNotFoundError,
)


ModelT = TypeVar("ModelT", bound=BaseModel)
RecordIdT = TypeVar("RecordIdT")
CandidateValidator = Callable[[Path], None]


class CsvRepository(Generic[ModelT, RecordIdT]):
    """Persist one Pydantic record type in a CSV file.

    Writes are serialized with a sidecar lock, validated before replacement,
    and committed with an atomic ``os.replace``. This implementation targets
    POSIX systems because it uses ``fcntl.flock`` and directory ``fsync``.
    """

    def __init__(
        self,
        *,
        path: Path,
        model_type: type[ModelT],
        fieldnames: Sequence[str],
        id_field: str,
        id_factory: Callable[[Sequence[ModelT]], RecordIdT],
        is_empty_id: Callable[[RecordIdT], bool],
        unique_keys: Sequence[Sequence[str]] = (),
        sort_key: Callable[[ModelT], Any] | None = None,
        candidate_validator: CandidateValidator | None = None,
        backup_limit: int = 20,
    ) -> None:
        if backup_limit < 0:
            raise ValueError("backup_limit cannot be negative")
        if id_field not in fieldnames:
            raise ValueError(f"id_field is not in CSV fields: {id_field}")

        self.path = Path(path)
        self.model_type = model_type
        self.fieldnames = tuple(fieldnames)
        self.id_field = id_field
        self.id_factory = id_factory
        self.is_empty_id = is_empty_id
        self.unique_keys = tuple(tuple(key) for key in unique_keys)
        self.sort_key = sort_key
        self.candidate_validator = candidate_validator
        self.backup_limit = backup_limit
        self._lock_path = self.path.parent / ".locks" / f"{self.path.name}.lock"
        self._backup_dir = self.path.parent / ".backups" / self.path.stem

    def all(self) -> list[ModelT]:
        """Read and parse every record in the current CSV file."""
        with self._locked():
            return self._read_unlocked()

    def get(self, record_id: RecordIdT) -> ModelT | None:
        """Read one record by its stable identifier."""
        with self._locked():
            return self._find_by_id(self._read_unlocked(), record_id)

    def create(self, record: ModelT) -> ModelT:
        """Create a record and assign an ID when the ID is empty."""
        with self._locked():
            records = self._read_unlocked()
            record_id = getattr(record, self.id_field)
            if self.is_empty_id(record_id):
                record_id = self.id_factory(records)
                record = record.model_copy(update={self.id_field: record_id})
            if self._find_by_id(records, record_id) is not None:
                raise RecordConflictError(f"Record ID already exists: {record_id}")

            candidate = [*records, record]
            self._write_candidate_unlocked(candidate)
            return record

    def update(self, record_id: RecordIdT, record: ModelT) -> ModelT:
        """Replace an existing record; never implicitly upsert."""
        with self._locked():
            records = self._read_unlocked()
            index = self._find_index_by_id(records, record_id)
            if index is None:
                raise RecordNotFoundError(f"Record does not exist: {record_id}")

            updated = record.model_copy(update={self.id_field: record_id})
            records[index] = updated
            self._write_candidate_unlocked(records)
            return updated

    def delete(self, record_id: RecordIdT) -> bool:
        """Physically delete a record by ID."""
        with self._locked():
            records = self._read_unlocked()
            index = self._find_index_by_id(records, record_id)
            if index is None:
                return False
            del records[index]
            self._write_candidate_unlocked(records)
            return True

    def replace_all(self, records: Iterable[ModelT]) -> list[ModelT]:
        """Replace the complete file snapshot, for migrations or recovery."""
        candidate = list(records)
        with self._locked():
            self._write_candidate_unlocked(candidate)
        return candidate

    @contextmanager
    def _locked(self):
        """Lock a sidecar file so atomic CSV replacement does not change the lock inode."""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self) -> list[ModelT]:
        if not self.path.exists():
            raise DataValidationError(f"CSV file does not exist: {self.path}")

        if self.candidate_validator is not None:
            self.candidate_validator(self.path)

        try:
            with self.path.open("r", newline="", encoding="utf-8-sig") as csv_file:
                reader = csv.DictReader(csv_file)
                actual_fields = tuple(reader.fieldnames or ())
                if actual_fields != self.fieldnames:
                    raise DataValidationError(
                        f"CSV header mismatch: expected={self.fieldnames}, actual={actual_fields}"
                    )
                records = [self.model_type.model_validate(row) for row in reader]
        except DataValidationError:
            raise
        except (OSError, csv.Error, ValidationError) as exc:
            raise DataValidationError(f"Failed to read CSV: {self.path}: {exc}") from exc

        self._validate_records(records)
        return records

    def _write_candidate_unlocked(self, records: list[ModelT]) -> None:
        records = [self.model_type.model_validate(record) for record in records]
        self._validate_records(records)
        if self.sort_key is not None:
            records.sort(key=self.sort_key)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                newline="",
                encoding="utf-8",
                prefix=f"{self.path.stem}.",
                suffix=".csv",
                dir=self.path.parent,
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                writer = csv.DictWriter(
                    temporary_file,
                    fieldnames=self.fieldnames,
                    lineterminator="\n",
                    extrasaction="raise",
                )
                writer.writeheader()
                for record in records:
                    writer.writerow(self._serialize(record))
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            if self.candidate_validator is not None:
                self.candidate_validator(temporary_path)
            self._backup_unlocked()
            os.replace(temporary_path, self.path)
            temporary_path = None
            self._fsync_parent_directory()
        except DataValidationError:
            raise
        except Exception as exc:
            raise DataValidationError(f"Failed to write CSV: {self.path}: {exc}") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _validate_records(self, records: Sequence[ModelT]) -> None:
        seen_ids: set[Any] = set()
        seen_unique: dict[tuple[str, ...], set[tuple[Any, ...]]] = {
            key: set() for key in self.unique_keys
        }

        for record in records:
            record_id = getattr(record, self.id_field)
            if self.is_empty_id(record_id):
                raise DataValidationError(f"Persisted record is missing ID field: {self.id_field}")
            if record_id in seen_ids:
                raise RecordConflictError(f"Duplicate record ID: {record_id}")
            seen_ids.add(record_id)

            for key_fields, seen_values in seen_unique.items():
                values = tuple(getattr(record, field) for field in key_fields)
                if values in seen_values:
                    labels = ", ".join(key_fields)
                    raise RecordConflictError(f"Duplicate unique key ({labels}): {values}")
                seen_values.add(values)

    def _serialize(self, record: ModelT) -> dict[str, Any]:
        values = record.model_dump(mode="json")
        return {
            field: "" if values.get(field) is None else values.get(field, "")
            for field in self.fieldnames
        }

    def _backup_unlocked(self) -> None:
        if self.backup_limit == 0 or not self.path.exists():
            return
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        shutil.copy2(self.path, self._backup_dir / f"{timestamp}.csv")
        backups = sorted(self._backup_dir.glob("*.csv"), reverse=True)
        for stale_backup in backups[self.backup_limit :]:
            stale_backup.unlink()

    def _fsync_parent_directory(self) -> None:
        directory_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _find_by_id(
        self,
        records: Sequence[ModelT],
        record_id: RecordIdT,
    ) -> ModelT | None:
        index = self._find_index_by_id(records, record_id)
        return records[index] if index is not None else None

    def _find_index_by_id(
        self,
        records: Sequence[ModelT],
        record_id: RecordIdT,
    ) -> int | None:
        for index, record in enumerate(records):
            if getattr(record, self.id_field) == record_id:
                return index
        return None
