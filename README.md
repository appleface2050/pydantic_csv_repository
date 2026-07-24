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
CSV fields, ID policy, sort order, and optional candidate validator.

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

## Persistence guarantees

Each repository uses a lock file next to the data file. A write is serialized,
written to a temporary file in the same directory, flushed, optionally validated,
backed up, and atomically replaced. A failed candidate validation leaves the
original CSV unchanged.

The repository expects the CSV file to exist before the first read or write. The
caller owns initial file creation and schema migration. `update()` never performs
an implicit insert, and `delete()` returns `False` when the ID does not exist.

## Development

```bash
python -m pip install -e '.[dev]'
pytest
ruff check .
python -m build
```

## License

MIT. See [LICENSE](LICENSE).
