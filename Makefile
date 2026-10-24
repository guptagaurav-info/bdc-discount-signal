PY ?= python
export PYTHONPATH := src

.PHONY: all ingest run test clean
all: ingest run
ingest:
	$(PY) -m bdcsignal.ingest
run:
	$(PY) -m bdcsignal.pipeline
test:
	$(PY) -m pytest -q tests
clean:
	rm -f reports/figures/sector_pnav_vs_nav_change.png reports/figures/discount_vs_nav_change.png reports/figures/nav_change_by_bucket.png reports/figures/income_spread.png reports/metrics.json reports/panel.csv reports/latest_snapshot.csv
