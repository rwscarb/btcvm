.PHONY: help install dev test lint fix run commit status clean completion

PYTHON     ?= python3
VENV_NAME  ?= btcvm
OTT      := $(PYTHON) ott.py
BTCVM      := $(PYTHON) main.py

help:
	@echo "btcvm — Bitcoin-clocked register machine"
	@echo ""
	@echo "Setup:"
	@echo "  make install       Install package (editable)"
	@echo "  make dev           Install with dev deps"
	@echo "  make completion    Install shell tab completions"
	@echo ""
	@echo "Testing:"
	@echo "  make test          Run all tests"
	@echo "  make lint          Run ruff linter"
	@echo "  make fix           Run ruff with --fix"
	@echo ""
	@echo "btcvm:"
	@echo "  make run           Run VM clock loop (one block)"
	@echo "  make run-vdf       Run VM with VDF sub-clock"
	@echo "  make run-trace     Run VM with trace Merkle"
	@echo "  make run-fleet     Run fleet of VMs"
	@echo "  make verify        Verify ledger against Bitcoin"
	@echo ""
	@echo "ott:"
	@echo "  make ott-status  Show archive status"
	@echo "  make ott-list    List archived files"
	@echo "  make ott-commit  Commit Merkle root to ledger"
	@echo "  make ott-clean   Remove manifest and ledger"
	@echo ""
	@echo "  make add FILE=path/to/file.jpg   Add file to archive"
	@echo "  make verify-file FILE=path       Verify file inclusion"

# ── Setup ─────────────────────────────────────────────────────────────────────

install:
	pip install -e .

dev:
	pip install -e ".[dev]"

completion:
	@mkdir -p ~/.local/share/bash-completion/completions
	@cp completions/btcvm.bash ~/.local/share/bash-completion/completions/btcvm
	@mkdir -p ~/.zsh/completions
	@cp completions/_btcvm ~/.zsh/completions/_btcvm
	@# Append zshrc lines only if not already present
	@grep -qF 'zsh/completions' ~/.zshrc 2>/dev/null || echo 'fpath=(~/.zsh/completions $$fpath)' >> ~/.zshrc
	@grep -qF 'autoload -Uz compinit' ~/.zshrc 2>/dev/null || echo 'autoload -Uz compinit && compinit' >> ~/.zshrc
	@# Bash: source line in .bashrc if not present
	@grep -qF 'bash-completion/completions/btcvm' ~/.bashrc 2>/dev/null || echo 'source ~/.local/share/bash-completion/completions/btcvm' >> ~/.bashrc
	@echo "✅ Completions installed."
	@echo "   Bash: restart shell or: source ~/.bashrc"
	@echo "   Zsh:  restart shell or: source ~/.zshrc"

# ── Tests + lint ──────────────────────────────────────────────────────────────

test:
	$(PYTHON) -m pytest test_vm.py -v

lint:
	ruff check .

fix:
	ruff check . --fix

# ── btcvm clock ───────────────────────────────────────────────────────────────

run:
	$(BTCVM) --once

run-vdf:
	$(BTCVM) --once --vdf-ticks 10

run-trace:
	$(BTCVM) --once --trace

run-fleet:
	$(BTCVM) --once --vms 4

verify:
	$(PYTHON) verify.py ledger.jsonl

# ── ott ─────────────────────────────────────────────────────────────────────

ott-status:
	$(OTT) status

ott-list:
	$(OTT) list

ott-commit:
	$(OTT) commit

ott-clean:
	@rm -f ott_manifest.jsonl ott_ledger.jsonl
	@echo "ott manifest and ledger removed."

add:
ifndef FILE
	$(error FILE is not set. Usage: make add FILE=path/to/file)
endif
	$(OTT) add $(FILE)

verify-file:
ifndef FILE
	$(error FILE is not set. Usage: make verify-file FILE=path/to/file)
endif
	$(OTT) verify $(FILE)

# ── Clean ─────────────────────────────────────────────────────────────────────

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -rf dist build *.egg-info .pytest_cache .ruff_cache
