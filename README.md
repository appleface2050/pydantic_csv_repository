# pydantic-csv-repository

Safe local CSV persistence for [Pydantic v2](https://docs.pydantic.dev/) models.

`pydantic-csv-repository` provides a small repository interface and a CSV adapter
that hides the difficult parts of file persistence:

- stable ID generation and duplicate checks;
- configurable business-key uniqueness checks;
- sidecar file locking that remains stable across atomic replacement;
- candidate validation before replacement;
- atomic `os.replace()` writes with file and directory `fsync`;
- configurable rolling backups.

The package is intentionally domain-agnostic. Callers provide the Pydantic model,
CSV fields, ID policy, sort order, and optional candidate validator. For the
0.1.x contract, `fieldnames` must exactly cover `model_type.model_fields`; fields
cannot be silently omitted or added as unknown CSV columns.

## Requirements

- Python 3.10+
- Pydantic 2.x
- Linux or macOS (the implementation uses POSIX `fcntl.flock`)

Install the core package:

```bash
pip install pydantic-csv-repository
```

Install the optional Frictionless validator adapter:

```bash
pip install 'pydantic-csv-repository[frictionless]'
```

## Example

```python
from pathlib import Path

from pydantic import BaseModel

from pydantic_csv_repository import CsvRepository


class Item(BaseModel):
    id: int = 0
    name: str
    value: float


path = Path("items.csv")
path.write_text("id,name,value\n", encoding="utf-8")

repository = CsvRepository(
    path=path,
    model_type=Item,
    fieldnames=("id", "name", "value"),
    id_field="id",
    id_factory=lambda records: max((item.id for item in records), default=0) + 1,
    is_empty_id=lambda record_id: record_id == 0,
    unique_keys=(("name",),),
    sort_key=lambda item: item.id,
    backup_limit=20,
)

created = repository.create(Item(name="alpha", value=1.5))
assert created.id == 1
assert repository.get(1) == created
```

## Field codecs

Fields without a codec use the legacy scalar CSV representation. Pydantic can
parse the stored text back into common scalar fields such as `str`, `int`,
`float`, and `bool`.

`None` values and structured values such as lists, dictionaries, and nested
models must use an explicit `FieldCodec`. This prevents ambiguous or lossy
serialization:

```python
import json

from pydantic_csv_repository import FieldCodec

json_codec = FieldCodec(
    encode=lambda value: json.dumps(value, ensure_ascii=False),
    decode=json.loads,
)

repository = CsvRepository(
    # ...other arguments...
    field_codecs={"metadata": json_codec},
)
```

An encoder must return `str`; a decoder receives the raw CSV text and returns
the Python value that should be passed to Pydantic validation. Before a write is
committed, the temporary CSV is decoded and validated through the repository's
own read path. The decoded snapshot must be semantically equal to the normalized
input snapshot, so lossy codecs are rejected before replacement.

## Frictionless validation

`FrictionlessResourceValidator` adapts a Frictionless Table Schema to the
`candidate_validator` hook. It validates both the current file before reads and
the temporary candidate before a write is committed:

```python
from pydantic_csv_repository import FrictionlessResourceValidator

repository = CsvRepository(
    # ...other arguments...
    candidate_validator=FrictionlessResourceValidator(schema_descriptor),
)
```

Validators must raise `DataValidationError` when a candidate is invalid.
Candidate validators are expected to be read-only; codec/Pydantic round-trip
validation remains the repository's responsibility.

## Persistence guarantees

Each repository uses a lock file next to the data file. A write is serialized,
written to a temporary file in the same directory, flushed, optionally validated,
backed up, and atomically replaced. A failed candidate validation leaves the
original CSV unchanged. CSV rows with missing or extra columns are rejected
before any write. Existing file permissions are preserved; new files use
`default_file_mode=0o600` unless configured otherwise.

If the replacement succeeds but the parent-directory `fsync` fails, the
repository raises `CommitDurabilityError` and explicitly reports that the file
may already be committed. Callers must inspect the file before retrying.

`create()`, `update()`, and `delete()` require the CSV file to exist because they
read the current snapshot first. `replace_all()` may initialize a missing CSV
file, which is useful for migrations and recovery. The caller owns schema
migration. `update()` never performs an implicit insert, and `delete()` returns
`False` when the ID does not exist.

## Development

```bash
python -m pip install -e '.[dev]'
pytest
ruff check .
python -m build
```

## License

MIT. See [LICENSE](LICENSE).
