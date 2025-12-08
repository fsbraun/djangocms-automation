# Makefile for Sphinx docs
# Minimal Sphinx Makefile adapted for Unix-like systems

SPHINXOPTS ?=
# Prefer sphinx-build from the local venv
VENV ?= .venv
SPHINXBUILD   = $(VENV)/bin/sphinx-build
SOURCEDIR     = source
BUILDDIR      = _build

PIP = $(VENV)/bin/pip
PYTHON = $(VENV)/bin/python

.PHONY: install venv

install: venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

venv:
	@test -d $(VENV) || python3 -m venv $(VENV)

# Explicit html target (uses the venv python -m sphinx to ensure correct interpreter)
html: venv
	$(PYTHON) -m sphinx -b html "$(SOURCEDIR)" "$(BUILDDIR)/html" $(SPHINXOPTS)

clean:
	@rm -rf "$(BUILDDIR)"/*
