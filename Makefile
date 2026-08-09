validate:
	python scripts/validate_registry.py
	python scripts/validate_seed_facts.py

materialize-datagouv:
	python scripts/materialize_data_gouv_ci.py --output data_lake

materialize-datagouv-sample:
	python scripts/materialize_data_gouv_ci.py --output data_lake --limit 5

test:
	python -m pytest -q
