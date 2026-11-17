PYTHON ?= $(if $(wildcard .venv-linux/bin/python),.venv-linux/bin/python,python3)
SRC = sqlcc.py

.PHONY: test coverage test-coverage

test:
	$(PYTHON) test.py

coverage:
	$(PYTHON) -m radon cc -s -a $(SRC)

test-coverage:
	$(PYTHON) -m coverage run --branch --include=$(SRC) test.py
	$(PYTHON) -m coverage report -m
