.PHONY: install run dev clean

VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

install: $(VENV)
	$(PIP) install -r requirements.txt

$(VENV):
	python3 -m venv $(VENV)

run: install
	$(VENV)/bin/uvicorn app.main:app --host 127.0.0.1 --port 8111

dev: install
	$(VENV)/bin/uvicorn app.main:app --host 127.0.0.1 --port 8111 --reload

clean:
	rm -rf $(VENV) __pycache__ app/__pycache__ app/**/__pycache__
