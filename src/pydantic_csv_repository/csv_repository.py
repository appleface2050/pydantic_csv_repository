"""Safe CSV repository implementation backed by Pydantic models."""

from __future__ import annotations

import csv
import datetime
import fcntl
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from pydantic_csv_repository.repository import (
    CommitDurabilityError,
    DataValidationError,
    RecordConflictError,
    RecordNotFoundError,
)


ModelT = TypeVar("ModelT", bound=BaseModel)
RecordIdT = TypeVar("RecordIdT")
CandidateValidator = Callable[[Path], None]


@dataclass(frozen=True)
class FieldCodec:
    """Encode one field to CSV text and decode it back to a Python value."""

    encode: Callable[[Any], str]
    decode: Callable[[str], Any]


class CsvRepository(Generic[ModelT, RecordIdT]):
    """Persist one Pydantic record type in a CSV file.

    Writes are serialized with a sidecar lock, validated before replacement,
    and committed with an atomic ``os.replace``. This implementation targets
    POSIX systems because it uses ``fcntl.flock`` and directory ``fsync``.

    Fields without a codec use the legacy scalar CSV representation. Values
    that are ``None`` or structured containers must provide an explicit codec;
    this prevents a successful write from producing an unreadable file.
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
        field_codecs: Mapping[str, FieldCodec] | None = None,
        default_file_mode: int = 0o600,
    ) -> None:
        normalized_fieldnames = tuple(fieldnames)
        if not isinstance(model_type, type) or not issubclass(model_type, BaseModel):
            raise ValueError("model_type must be a Pydantic BaseModel subclass")
        aliased_fields = sorted(
            field_name
            for field_name, field_info in model_type.model_fields.items()
            if field_info.alias not in (None, field_name)
            or field_info.validation_alias is not None
            or field_info.serialization_alias not in (None, field_name)
        )
        if aliased_fields:
            raise ValueError(
                "model_type fields with aliases are not supported: "
                f"{aliased_fields}"
            )
        model_fields = set(model_type.model_fields)
        if not normalized_fieldnames:
            raise ValueError("fieldnames cannot be empty")
        if any(not isinstance(field, str) or not field for field in normalized_fieldnames):
            raise ValueError("fieldnames must contain non-empty strings")
        fieldname_set = set(normalized_fieldnames)
        if len(fieldname_set) != len(normalized_fieldnames):
            raise ValueError("fieldnames contains duplicates")
        missing_model_fields = model_fields - fieldname_set
        unknown_model_fields = fieldname_set - model_fields
        if missing_model_fields or unknown_model_fields:
            raise ValueError(
                "fieldnames must exactly cover model fields; "
                f"missing={sorted(missing_model_fields)}, unknown={sorted(unknown_model_fields)}"
            )
        if id_field not in normalized_fieldnames:
            raise ValueError(f"id_field is not in CSV fields: {id_field}")
        if id_field not in model_fields:
            raise ValueError(f"id_field is not a model field: {id_field}")
        if isinstance(backup_limit, bool) or not isinstance(backup_limit, int) or backup_limit < 0:
            raise ValueError("backup_limit must be a non-negative integer")
        if isinstance(default_file_mode, bool) or not isinstance(default_file_mode, int):
            raise ValueError("default_file_mode must be an integer")
        if not 0 <= default_file_mode <= 0o777:
            raise ValueError("default_file_mode must be between 0 and 0o777")
        if not callable(id_factory) or not callable(is_empty_id):
            raise ValueError("id_factory and is_empty_id must be callable")
        if sort_key is not None and not callable(sort_key):
            raise ValueError("sort_key must be callable")
        if candidate_validator is not None and not callable(candidate_validator):
            raise ValueError("candidate_validator must be callable")

        normalized_unique_keys = tuple(tuple(key) for key in unique_keys)
        for key in normalized_unique_keys:
            if not key:
                raise ValueError("unique_keys cannot contain an empty key")
            if any(not isinstance(field, str) or not field for field in key):
                raise ValueError("unique_keys must contain non-empty string field names")
            unknown_fields = set(key) - fieldname_set
            if unknown_fields:
                raise ValueError(f"unique key contains unknown CSV fields: {unknown_fields}")
            unknown_model_fields = set(key) - model_fields
            if unknown_model_fields:
                raise ValueError(f"unique key contains unknown model fields: {unknown_model_fields}")

        normalized_codecs = dict(field_codecs or {})
        unknown_codec_fields = set(normalized_codecs) - fieldname_set
        if unknown_codec_fields:
            raise ValueError(f"field_codecs contains unknown CSV fields: {unknown_codec_fields}")
        for field, codec in normalized_codecs.items():
            if not callable(getattr(codec, "encode", None)) or not callable(
                getattr(codec, "decode", None)
            ):
                raise ValueError(f"codec for {field!r} must provide callable encode/decode")

        self.path = Path(path)
        self.model_type = model_type
        self.fieldnames = normalized_fieldnames
        self.id_field = id_field
        self.id_factory = id_factory
        self.is_empty_id = is_empty_id
        self.unique_keys = normalized_unique_keys
        self.sort_key = sort_key
        self.candidate_validator = candidate_validator
        self.backup_limit = backup_limit
        self.field_codecs = normalized_codecs
        self.default_file_mode = default_file_mode
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
            normalized_record = self._normalize_record(record)
            record_id = getattr(normalized_record, self.id_field)
            if self.is_empty_id(record_id):
                factory_snapshot = tuple(record.model_copy(deep=True) for record in records)
                try:
                    generated_id = self.id_factory(factory_snapshot)
                except Exception as exc:
                    raise DataValidationError(f"id_factory failed: {exc}") from exc
                normalized_record = self._with_updates(
                    normalized_record,
                    {self.id_field: generated_id},
                )
                record_id = getattr(normalized_record, self.id_field)
            if self.is_empty_id(record_id):
                raise DataValidationError(f"Generated record ID is empty: {self.id_field}")
            if self._find_by_id(records, record_id) is not None:
                raise RecordConflictError(f"Record ID already exists: {record_id}")

            persisted = self._write_candidate_unlocked([*records, normalized_record])
            return self._get_required_record(persisted, record_id)

    def update(self, record_id: RecordIdT, record: ModelT) -> ModelT:
        """Replace an existing record; never implicitly upsert."""
        with self._locked():
            records = self._read_unlocked()
            index = self._find_index_by_id(records, record_id)
            if index is None:
                raise RecordNotFoundError(f"Record does not exist: {record_id}")

            records[index] = self._with_updates(record, {self.id_field: record_id})
            persisted = self._write_candidate_unlocked(records)
            return self._get_required_record(persisted, record_id)

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
        """Replace the complete file snapshot and return its final form."""
        with self._locked():
            return self._write_candidate_unlocked(list(records))

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
        return self._read_path_unlocked(self.path, run_candidate_validator=True)

    def _read_path_unlocked(
        self,
        path: Path,
        *,
        run_candidate_validator: bool,
    ) -> list[ModelT]:
        """Read one CSV path through the complete repository decoding pipeline."""
        if not path.exists():
            raise DataValidationError(f"CSV file does not exist: {path}")
        try:
            if run_candidate_validator and self.candidate_validator is not None:
                self.candidate_validator(path)

            with path.open("r", newline="", encoding="utf-8-sig") as csv_file:
                reader = csv.DictReader(csv_file, strict=True)
                actual_fields = tuple(reader.fieldnames or ())
                if actual_fields != self.fieldnames:
                    raise DataValidationError(
                        f"CSV header mismatch: expected={self.fieldnames}, actual={actual_fields}"
                    )

                records = []
                for row in reader:
                    line_number = reader.line_num
                    extra_values = row.get(None)
                    if extra_values is not None:
                        raise DataValidationError(
                            f"CSV row {line_number} contains extra columns: {extra_values}"
                        )
                    missing_fields = [
                        field for field in self.fieldnames if row.get(field) is None
                    ]
                    if missing_fields:
                        raise DataValidationError(
                            f"CSV row {line_number} is missing columns: {missing_fields}"
                        )
                    records.append(self._deserialize_row(row, line_number))
        except (DataValidationError, RecordConflictError):
            raise
        except (OSError, csv.Error, ValidationError) as exc:
            raise DataValidationError(f"Failed to read CSV: {path}: {exc}") from exc
        except Exception as exc:
            raise DataValidationError(f"Failed to read CSV: {path}: {exc}") from exc

        try:
            self._validate_records(records)
        except (DataValidationError, RecordConflictError):
            raise
        except Exception as exc:
            raise DataValidationError(f"Failed to validate CSV records: {path}: {exc}") from exc
        return records

    def _write_candidate_unlocked(self, records: list[ModelT]) -> list[ModelT]:
        try:
            normalized_records = [self._normalize_record(record) for record in records]
            self._validate_records(normalized_records)
            if self.sort_key is not None:
                normalized_records.sort(key=self.sort_key)
        except (DataValidationError, RecordConflictError):
            raise
        except Exception as exc:
            raise DataValidationError(f"Failed to prepare CSV candidate: {self.path}: {exc}") from exc

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            existing_mode = (
                stat.S_IMODE(self.path.stat().st_mode)
                if self.path.exists()
                else self.default_file_mode
            )
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
                for record in normalized_records:
                    writer.writerow(self._serialize(record))
                os.fchmod(temporary_file.fileno(), existing_mode)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            if self.candidate_validator is not None:
                self.candidate_validator(temporary_path)
            candidate_records = self._read_path_unlocked(
                temporary_path,
                run_candidate_validator=False,
            )
            self._assert_round_trip(normalized_records, candidate_records)
            self._backup_unlocked()
            os.replace(temporary_path, self.path)
            temporary_path = None
            try:
                self._fsync_parent_directory()
            except OSError as exc:
                raise CommitDurabilityError(
                    f"CSV was replaced, but directory fsync failed: {self.path}; "
                    "the file may already be committed and should not be blindly retried"
                ) from exc
        except (DataValidationError, RecordConflictError, CommitDurabilityError):
            raise
        except Exception as exc:
            raise DataValidationError(f"Failed to write CSV: {self.path}: {exc}") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        return candidate_records

    def _normalize_record(self, record: Any) -> ModelT:
        try:
            values = (
                record.model_dump(mode="python", round_trip=True)
                if isinstance(record, BaseModel)
                else record
            )
            return self.model_type.model_validate(values)
        except ValidationError as exc:
            raise DataValidationError(f"Record validation failed: {exc}") from exc
        except Exception as exc:
            raise DataValidationError(f"Record cannot be normalized: {exc}") from exc

    def _with_updates(self, record: ModelT, updates: Mapping[str, Any]) -> ModelT:
        values = record.model_dump(mode="python", round_trip=True)
        values.update(updates)
        return self._normalize_record(values)

    def _deserialize_row(self, row: Mapping[str | None, str], line_number: int) -> ModelT:
        values: dict[str, Any] = {}
        try:
            for field in self.fieldnames:
                raw_value = row[field]
                codec = self.field_codecs.get(field)
                values[field] = codec.decode(raw_value) if codec is not None else raw_value
            return self._normalize_record(values)
        except DataValidationError as exc:
            raise DataValidationError(f"CSV row {line_number} is invalid: {exc}") from exc
        except Exception as exc:
            raise DataValidationError(
                f"CSV row {line_number} field decoding failed: {exc}"
            ) from exc

    def _assert_round_trip(
        self,
        expected_records: Sequence[ModelT],
        decoded_records: Sequence[ModelT],
    ) -> None:
        """Reject codecs whose persisted representation changes model semantics."""
        expected_values = [
            record.model_dump(mode="python", round_trip=True)
            for record in expected_records
        ]
        decoded_values = [
            record.model_dump(mode="python", round_trip=True)
            for record in decoded_records
        ]
        if expected_values != decoded_values:
            raise DataValidationError(
                "CSV codec round-trip changed the normalized snapshot; "
                "encode/decode must preserve model semantics"
            )

    def _validate_records(self, records: Sequence[ModelT]) -> None:
        seen_ids: set[Any] = set()
        seen_unique: dict[tuple[str, ...], set[tuple[Any, ...]]] = {
            key: set() for key in self.unique_keys
        }

        for record in records:
            record_id = getattr(record, self.id_field)
            if self.is_empty_id(record_id):
                raise DataValidationError(f"Persisted record is missing ID field: {self.id_field}")
            try:
                if record_id in seen_ids:
                    raise RecordConflictError(f"Duplicate record ID: {record_id}")
                seen_ids.add(record_id)
            except TypeError as exc:
                raise DataValidationError("Record ID must be hashable") from exc

            for key_fields, seen_values in seen_unique.items():
                values = tuple(getattr(record, field) for field in key_fields)
                try:
                    if values in seen_values:
                        labels = ", ".join(key_fields)
                        raise RecordConflictError(
                            f"Duplicate unique key ({labels}): {values}"
                        )
                    seen_values.add(values)
                except TypeError as exc:
                    labels = ", ".join(key_fields)
                    raise DataValidationError(
                        f"Unique key ({labels}) must contain hashable values"
                    ) from exc

    def _serialize(self, record: ModelT) -> dict[str, str]:
        python_values = record.model_dump(mode="python", round_trip=True)
        json_values = record.model_dump(mode="json", round_trip=True)
        serialized: dict[str, str] = {}
        for field in self.fieldnames:
            codec = self.field_codecs.get(field)
            if codec is not None:
                try:
                    encoded = codec.encode(python_values[field])
                except Exception as exc:
                    raise DataValidationError(
                        f"Failed to encode field {field!r}: {exc}"
                    ) from exc
            else:
                value = json_values[field]
                if value is None:
                    raise DataValidationError(
                        f"Field {field!r} is None and requires an explicit codec"
                    )
                if isinstance(value, (list, dict, tuple, set)):
                    raise DataValidationError(
                        f"Field {field!r} is structured and requires an explicit codec"
                    )
                if not isinstance(value, (str, int, float, bool)):
                    raise DataValidationError(
                        f"Field {field!r} has unsupported CSV value type: {type(value).__name__}"
                    )
                encoded = value if isinstance(value, str) else str(value)

            if not isinstance(encoded, str):
                raise DataValidationError(f"Codec for field {field!r} must return str")
            serialized[field] = encoded
        return serialized

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

    def _get_required_record(
        self,
        records: Sequence[ModelT],
        record_id: RecordIdT,
    ) -> ModelT:
        record = self._find_by_id(records, record_id)
        if record is None:
            raise DataValidationError(f"Persisted record cannot be found: {record_id}")
        return record

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
