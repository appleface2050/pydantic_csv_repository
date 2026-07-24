"""Behaviour tests for the public CsvRepository implementation."""

from pathlib import Path

import pytest
from pydantic import BaseModel

from pydantic_csv_repository import (
    CsvRepository,
    DataValidationError,
    FrictionlessResourceValidator,
    RecordConflictError,
    RecordNotFoundError,
)


class Item(BaseModel):
    """Minimal test record."""

    id: int = 0
    name: str
    value: float


SCHEMA = {
    "fields": [
        {"name": "id", "type": "integer", "constraints": {"required": True}},
        {"name": "name", "type": "string", "constraints": {"required": True}},
        {"name": "value", "type": "number", "constraints": {"required": True}},
    ],
    "primaryKey": "id",
}


def _build_repository(path: Path, *, validator=None, backup_limit: int = 20):
    return CsvRepository(
        path=path,
        model_type=Item,
        fieldnames=("id", "name", "value"),
        id_field="id",
        id_factory=lambda records: max((item.id for item in records), default=0) + 1,
        is_empty_id=lambda record_id: record_id == 0,
        unique_keys=(("name",),),
        sort_key=lambda item: item.id,
        candidate_validator=validator,
        backup_limit=backup_limit,
    )


def test_crud_uses_stable_ids_and_persists_valid_csv(tmp_path):
    path = tmp_path / "items.csv"
    path.write_text("id,name,value\n", encoding="utf-8")
    repository = _build_repository(
        path,
        validator=FrictionlessResourceValidator(SCHEMA),
    )

    first = repository.create(Item(name="alpha", value=1.5))
    second = repository.create(Item(name="beta", value=2.5))
    updated = repository.update(second.id, Item(name="beta-2", value=3.5))

    assert first.id == 1
    assert second.id == 2
    assert updated.id == 2
    assert repository.get(1) == first
    assert repository.delete(1) is True
    assert repository.delete(1) is False
    assert repository.all() == [updated]
    assert path.read_text(encoding="utf-8").splitlines()[0] == "id,name,value"


def test_create_rejects_unique_key_conflict(tmp_path):
    path = tmp_path / "items.csv"
    path.write_text("id,name,value\n1,alpha,1\n", encoding="utf-8")
    repository = _build_repository(path)

    with pytest.raises(RecordConflictError):
        repository.create(Item(name="alpha", value=2))


def test_update_missing_record_does_not_upsert(tmp_path):
    path = tmp_path / "items.csv"
    path.write_text("id,name,value\n", encoding="utf-8")
    repository = _build_repository(path)

    with pytest.raises(RecordNotFoundError):
        repository.update(9, Item(name="missing", value=1))

    assert repository.all() == []


def test_read_fails_closed_after_invalid_manual_edit(tmp_path):
    path = tmp_path / "items.csv"
    path.write_text("id,name,value\n1,,1\n", encoding="utf-8")
    repository = _build_repository(
        path,
        validator=FrictionlessResourceValidator(SCHEMA),
    )

    with pytest.raises(DataValidationError, match="Frictionless validation failed"):
        repository.all()


def test_failed_candidate_validation_preserves_original_file(tmp_path):
    path = tmp_path / "items.csv"
    original = "id,name,value\n1,alpha,1\n"
    path.write_text(original, encoding="utf-8")

    def reject_candidate(_path):
        raise DataValidationError("reject")

    repository = _build_repository(path, validator=reject_candidate)

    with pytest.raises(DataValidationError, match="reject"):
        repository.create(Item(name="beta", value=2))

    assert path.read_text(encoding="utf-8") == original


def test_write_keeps_only_configured_number_of_backups(tmp_path):
    path = tmp_path / "items.csv"
    path.write_text("id,name,value\n", encoding="utf-8")
    repository = _build_repository(path, backup_limit=2)

    repository.create(Item(name="one", value=1))
    repository.create(Item(name="two", value=2))
    repository.create(Item(name="three", value=3))

    backup_dir = tmp_path / ".backups" / "items"
    assert len(list(backup_dir.glob("*.csv"))) == 2
