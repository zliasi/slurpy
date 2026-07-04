.PHONY: test goldens check

test:
	python3 -m unittest discover -s tests

goldens:
	python3 tests/update-goldens.py

PY_FILES = slurpy.py migrate.py tests/test_slurpy.py tests/test_commands.py \
	tests/test_migrate.py tests/update-goldens.py

check:
	black --check $(PY_FILES)
	ruff check $(PY_FILES)
	mypy --strict slurpy.py migrate.py
