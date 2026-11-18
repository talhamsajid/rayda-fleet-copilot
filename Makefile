ENV := $(if $(wildcard .env),--env-file .env,)
RUN := uv run $(ENV)
EVAL_MODEL ?= gemini:gemini-3.6-flash
MODEL ?= $(EVAL_MODEL)
RPM ?= 10
REPEATS ?= 1
TENANT ?= acme-001

.PHONY: setup test eval eval-record eval-live chat studio web briefing

setup:
	uv sync

test:
	uv run pytest -q

eval:
	$(RUN) python -m evals.run --mode replay --models $(EVAL_MODEL)

eval-record:
	FLEET_RPM=$(RPM) $(RUN) python -m evals.run --mode record --models $(EVAL_MODEL)

eval-live:
	FLEET_RPM=$(RPM) $(RUN) python -m evals.run --mode live --models $(MODEL) --repeats $(REPEATS) --out $(or $(OUT),runs)

chat:
	$(RUN) fleet-copilot --tenant $(TENANT)

studio:
	$(RUN) langgraph dev

web:
	$(RUN) fleet-copilot-web

briefing:
	$(RUN) fleet-copilot-briefing --tenant all
