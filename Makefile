.PHONY: test goldens check

test:
	python3 -m unittest discover -s tests

goldens:
	python3 tests/update-goldens.py

check:
	black --check slurpy.py tests/test_slurpy.py tests/update-goldens.py
	ruff check slurpy.py tests/test_slurpy.py tests/update-goldens.py
	mypy --strict slurpy.py
