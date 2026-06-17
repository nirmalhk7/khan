SHELL := /bin/sh

PYTHON ?= python3
VENV := .venv
VENV_PYTHON := $(VENV)/bin/python
VENV_PIP := $(VENV)/bin/pip
INSTALL_DIR ?= $(HOME)/.local/bin
KHAN_LINK := $(INSTALL_DIR)/khan

.PHONY: setup test

setup: $(VENV_PYTHON)
	$(VENV_PIP) install -r requirements.txt
	chmod +x khan
	mkdir -p "$(INSTALL_DIR)"
	ln -sf "$(CURDIR)/khan" "$(KHAN_LINK)"
	"$(KHAN_LINK)" init
	"$(KHAN_LINK)" doctor
	@printf '\nKhan is installed at %s\n' "$(KHAN_LINK)"

$(VENV_PYTHON):
	$(PYTHON) -m venv "$(VENV)"

test:
	$(VENV_PYTHON) -W error::ResourceWarning -m unittest discover -s tests -v
