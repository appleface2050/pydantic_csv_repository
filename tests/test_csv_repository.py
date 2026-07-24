"""Behaviour and safety tests for the public CsvRepository implementation."""

import json
import multiprocessing
import os
from pathlib import Path
from stat import S_IMODE

import pytest
from pydantic import BaseModel, field_validator

from pydantic_csv_repository import (
    CommitDurabilityError,
    CsvRepository,
    DataValidationError,
    FieldCodec,
    FrictionlessResourceValidator,
    RecordConflictError,
    RecordNotFoundError,
)


class Item(BaseModel):
    """Minimal test record."""

    id: int = 0
    name: str
    value: float


class ValidatedItem(BaseModel):
    """Record whose field validator must run after ID assignment."""

    id: int = 0
    name: str

    @field_validator("name")
    @classmethod
    def name_must_not_be_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("name cannot be empty")
        return value


class NullableItem(BaseModel):
    """Record used to verify explicit codecs for nullable and structured fields."""

    id: int = 0
    note: int | None = None
    tags: list[str] = []


class TextItem(BaseModel):
    """Record used to test lossy text codecs."""

    id: int = 0
    note: str


class RequiredFieldItem(BaseModel):
    """Record with a required field that must not be silently omitted."""

    id: int = 0
    name: str
    secret: str


class DefaultedFieldItem(BaseModel):
    """Record with a defaulted field that must not be silently omitted."""

    id: int = 0
    name: str
    metadata: str = "default"


SCHEMA = {
    "fields": [
        {"name": "id", "type": "integer", "constraints": {"required": True}},
        {"name": "name", "type": "string", "constraints": {"required": True}},
        {"name": "value", "type": "number", "constraints": {"required": True}},
    ],
    "primaryKey": "id",
}


def _build_repository(
    path: Path,
    *,
    validator=None,
    backup_limit: int = 20,
    id_factory=None,
):
    return CsvRepository(
        path=path,
        model_type=Item,
        fieldnames=("id", "name", "value"),
        id_field="id",
        id_factory=id_factory or (lambda records: max((item.id for item in records), default=0) + 1),
        is_empty_id=lambda record_id: record_id == 0,
        unique_keys=(("name",),),
        sort_key=lambda item: item.id,
        candidate_validator=validator,
        backup_limit=backup_limit,
    )


def _build_nullable_repository(path: Path, *, field_codecs=None):
    return CsvRepository(
        path=path,
        model_type=NullableItem,
        fieldnames=("id", "note", "tags"),
        id_field="id",
        id_factory=lambda records: max((item.id for item in records), default=0) + 1,
        is_empty_id=lambda record_id: record_id == 0,
        sort_key=lambda item: item.id,
        field_codecs=field_codecs,
    )


def _create_item_in_process(path: str, name: str) -> None:
    """Create one item from a separate process for the flock regression test."""
    repository = _build_repository(Path(path), backup_limit=0)
    repository.create(Item(name=name, value=1))


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


def test_create_rejects_invalid_generated_id(tmp_path):
    path = tmp_path / "items.csv"
    path.write_text("id,name,value\n", encoding="utf-8")
    repository = _build_repository(path, id_factory=lambda _records: "not-an-int")

    with pytest.raises(DataValidationError, match="Record validation failed"):
        repository.create(Item(name="alpha", value=1))

    assert repository.all() == []


def test_model_instance_is_revalidated_before_create(tmp_path):
    path = tmp_path / "items.csv"
    path.write_text("id,name\n", encoding="utf-8")
    repository = CsvRepository(
        path=path,
        model_type=ValidatedItem,
        fieldnames=("id", "name"),
        id_field="id",
        id_factory=lambda records: max((item.id for item in records), default=0) + 1,
        is_empty_id=lambda record_id: record_id == 0,
    )
    invalid_record = ValidatedItem.model_construct(id=1, name="")

    with pytest.raises(DataValidationError, match="Record validation failed"):
        repository.create(invalid_record)

    assert repository.all() == []


def test_update_revalidates_model_after_forcing_id(tmp_path):
    path = tmp_path / "items.csv"
    path.write_text("id,name\n1,alpha\n", encoding="utf-8")
    repository = CsvRepository(
        path=path,
        model_type=ValidatedItem,
        fieldnames=("id", "name"),
        id_field="id",
        id_factory=lambda records: max((item.id for item in records), default=0) + 1,
        is_empty_id=lambda record_id: record_id == 0,
    )
    invalid_record = ValidatedItem.model_construct(id=0, name="")

    with pytest.raises(DataValidationError, match="Record validation failed"):
        repository.update(1, invalid_record)

    assert repository.all() == [ValidatedItem(id=1, name="alpha")]


def test_nullable_and_structured_fields_require_explicit_codecs(tmp_path):
    path = tmp_path / "items.csv"
    path.write_text("id,note,tags\n", encoding="utf-8")
    repository = _build_nullable_repository(path)

    with pytest.raises(DataValidationError, match="requires an explicit codec"):
        repository.create(NullableItem(note=None, tags=["alpha"]))


def test_json_codecs_preserve_nullable_and_structured_values(tmp_path):
    path = tmp_path / "items.csv"
    path.write_text("id,note,tags\n", encoding="utf-8")
    json_codec = FieldCodec(
        encode=lambda value: json.dumps(value, ensure_ascii=False),
        decode=json.loads,
    )
    repository = _build_nullable_repository(
        path,
        field_codecs={"note": json_codec, "tags": json_codec},
    )

    created = repository.create(NullableItem(note=None, tags=["alpha", "beta"]))

    assert repository.all() == [created]
    assert repository.all()[0].note is None
    assert repository.all()[0].tags == ["alpha", "beta"]


def test_bad_codec_decoder_is_rejected_before_replace(tmp_path):
    path = tmp_path / "items.csv"
    path.write_text("id,note,tags\n", encoding="utf-8")
    bad_codec = FieldCodec(
        encode=lambda value: json.dumps(value),
        decode=lambda _text: (_ for _ in ()).throw(ValueError("bad decoder")),
    )
    repository = _build_nullable_repository(
        path,
        field_codecs={"note": FieldCodec(encode=str, decode=int), "tags": bad_codec},
    )

    with pytest.raises(DataValidationError, match="field decoding failed"):
        repository.create(NullableItem(note=1, tags=["alpha"]))

    assert path.read_text(encoding="utf-8") == "id,note,tags\n"


def test_codec_decode_result_must_pass_pydantic_before_replace(tmp_path):
    path = tmp_path / "items.csv"
    path.write_text("id,note,tags\n", encoding="utf-8")
    invalid_note_codec = FieldCodec(
        encode=str,
        decode=lambda _text: "not-an-int",
    )
    json_codec = FieldCodec(
        encode=lambda value: json.dumps(value),
        decode=json.loads,
    )
    repository = _build_nullable_repository(
        path,
        field_codecs={"note": invalid_note_codec, "tags": json_codec},
    )

    with pytest.raises(DataValidationError, match="Record validation failed"):
        repository.create(NullableItem(note=1, tags=[]))

    assert repository.all() == []


def test_lossy_codec_is_rejected_before_replace(tmp_path):
    path = tmp_path / "items.csv"
    path.write_text("id,note\n", encoding="utf-8")
    lossy_codec = FieldCodec(encode=str.upper, decode=str.lower)
    repository = CsvRepository(
        path=path,
        model_type=TextItem,
        fieldnames=("id", "note"),
        id_field="id",
        id_factory=lambda records: max((item.id for item in records), default=0) + 1,
        is_empty_id=lambda record_id: record_id == 0,
        field_codecs={"note": lossy_codec},
    )

    with pytest.raises(DataValidationError, match="round-trip changed"):
        repository.create(TextItem(note="MiXeD"))

    assert repository.all() == []


@pytest.mark.parametrize(
    ("model_type", "fieldnames"),
    [
        (RequiredFieldItem, ("id", "name")),
        (DefaultedFieldItem, ("id", "name")),
        (Item, ("id", "name", "value", "legacy")),
    ],
)
def test_fieldnames_must_exactly_cover_model_fields(tmp_path, model_type, fieldnames):
    with pytest.raises(ValueError, match="fieldnames must exactly cover model fields"):
        CsvRepository(
            path=tmp_path / "items.csv",
            model_type=model_type,
            fieldnames=fieldnames,
            id_field="id",
            id_factory=lambda records: 1,
            is_empty_id=lambda record_id: record_id == 0,
        )


def test_strict_csv_parser_rejects_malformed_quoted_field(tmp_path):
    path = tmp_path / "items.csv"
    path.write_text('id,name,value\n1,"a"b,2\n', encoding="utf-8")
    repository = _build_repository(path)

    with pytest.raises(DataValidationError):
        repository.all()


def test_file_mode_is_applied_before_file_fsync(tmp_path, monkeypatch):
    path = tmp_path / "items.csv"
    path.write_text("id,name,value\n", encoding="utf-8")
    repository = _build_repository(path)
    events = []

    monkeypatch.setattr(os, "fchmod", lambda _fd, _mode: events.append("fchmod"))
    monkeypatch.setattr(os, "fsync", lambda _fd: events.append("fsync"))

    repository.create(Item(name="alpha", value=1))

    assert events[:2] == ["fchmod", "fsync"]


def test_read_rejects_extra_csv_columns_before_any_write(tmp_path):
    path = tmp_path / "items.csv"
    path.write_text("id,name,value\n1,alpha,1,UNSEEN\n", encoding="utf-8")
    repository = _build_repository(path)

    with pytest.raises(DataValidationError, match="extra columns"):
        repository.all()


def test_read_rejects_missing_csv_columns(tmp_path):
    path = tmp_path / "items.csv"
    path.write_text("id,name,value\n1,alpha\n", encoding="utf-8")
    repository = _build_repository(path)

    with pytest.raises(DataValidationError, match="missing columns"):
        repository.all()


def test_directory_fsync_failure_reports_uncertain_commit(tmp_path, monkeypatch):
    path = tmp_path / "items.csv"
    path.write_text("id,name,value\n", encoding="utf-8")
    repository = _build_repository(path)

    def fail_fsync():
        raise OSError("simulated directory fsync failure")

    monkeypatch.setattr(repository, "_fsync_parent_directory", fail_fsync)

    with pytest.raises(CommitDurabilityError, match="may already be committed"):
        repository.create(Item(name="alpha", value=1))

    assert repository.all() == [Item(id=1, name="alpha", value=1)]


def test_existing_file_mode_is_preserved(tmp_path):
    path = tmp_path / "items.csv"
    path.write_text("id,name,value\n", encoding="utf-8")
    os.chmod(path, 0o640)
    repository = _build_repository(path)

    repository.create(Item(name="alpha", value=1))

    assert S_IMODE(path.stat().st_mode) == 0o640


def test_replace_all_returns_sorted_normalized_snapshot(tmp_path):
    path = tmp_path / "items.csv"
    path.write_text("id,name,value\n", encoding="utf-8")
    repository = _build_repository(path)

    persisted = repository.replace_all(
        [Item(id=2, name="beta", value=2), Item(id=1, name="alpha", value=1)]
    )

    assert persisted == repository.all()
    assert [item.id for item in persisted] == [1, 2]


@pytest.mark.skipif(not hasattr(multiprocessing, "get_context"), reason="POSIX process test")
def test_concurrent_process_creates_keep_unique_ids(tmp_path):
    path = tmp_path / "items.csv"
    path.write_text("id,name,value\n", encoding="utf-8")
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_create_item_in_process, args=(str(path), f"item-{index}"))
        for index in range(4)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)

    assert all(process.exitcode == 0 for process in processes)
    records = _build_repository(path, backup_limit=0).all()
    assert len(records) == 4
    assert {record.id for record in records} == {1, 2, 3, 4}
