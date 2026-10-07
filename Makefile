PYTHON ?= python3

.PHONY: acceptance
acceptance:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'
