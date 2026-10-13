PY ?= python

.PHONY: install data test lint ask

install:
	$(PY) -m pip install -e ".[dev]"

data:  ## regenerate the synthetic directory + corpus (deterministic)
	$(PY) -m scripts.generate_data

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check . && $(PY) -m ruff format --check .

ask:  ## make ask USER=erin Q="what discount can I offer?"
	$(PY) -m rag.cli --user $(USER) --debug "$(Q)"

# run / eval / deploy targets are added in later phases.
