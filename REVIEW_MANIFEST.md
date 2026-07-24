# External Review Manifest

- Source commit: `7c9bb2635e3e7168af57e71f855bb667d895d5c1`
- GitHub: `appleface2050/pydantic_csv_repository`
- Package version: `0.1.0` (not released to PyPI)
- Requirements: Python `>=3.10`, Linux/macOS, Pydantic 2.x
- Local Python: `3.13.5`
- Package tests: `python -m pytest tests -q` → `25 passed`
- Repository integration tests: `python -m pytest packages/pydantic-csv-repository/tests Running/tests/test_repository_readers.py Web-dashboard/tests/test_running_management_repository.py -q` → `30 passed`
- Ruff: `ruff check .` → passed
- Build: `python -m build` → sdist and pure-Python wheel passed
- Archive SHA-256: provided with the review archive; not embedded to avoid a self-referential hash

Install development dependencies before reproducing the checks:

```bash
python -m pip install -e '.[dev]'
```
