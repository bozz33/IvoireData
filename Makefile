validate:
	python scripts/validate_registry.py

summary:
	python scripts/build_summary.py

manifest:
	python scripts/build_manifest.py --collection civ_open_stats

test:
	python -m pytest -q
