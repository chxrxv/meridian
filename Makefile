PY ?= python

.PHONY: install data test lint ask eval eval-smoke report

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

TAG ?= dev
eval:  ## full golden set through the pipeline: make eval TAG=baseline-extractive
	$(PY) -m evals.run --tag $(TAG)
	$(PY) -m evals.report $(TAG)

eval-smoke:  ## 6 scenarios, mocked model, no credentials
	$(PY) -m evals.run --subset 6 --tag smoke --generator mock --store memory
	$(PY) -m evals.report smoke

report:  ## make report TAG=new COMPARE=baseline-extractive
	$(PY) -m evals.report $(TAG) $(if $(COMPARE),--compare $(COMPARE),)

# run / deploy targets are added in later phases.
