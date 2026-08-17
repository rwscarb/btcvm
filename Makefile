.PHONY: help install dev test lint fix run commit status clean completion

PYTHON     ?= python3
VENV_NAME  ?= btcvm
IMGFS      := $(PYTHON) imgfs.py
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
	@echo "imgfs:"
	@echo "  make imgfs-status  Show archive status"
	@echo "  make imgfs-list    List archived files"
	@echo "  make imgfs-commit  Commit Merkle root to ledger"
	@echo "  make imgfs-clean   Remove manifest and ledger"
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
	@echo "Bash: source ~/.local/share/bash-completion/completions/btcvm"
	@echo "Zsh:  fpath=(~/.zsh/completions \$$fpath) in ~/.zshrc, then compinit"

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

# ── imgfs ─────────────────────────────────────────────────────────────────────

imgfs-status:
	$(IMGFS) status

imgfs-list:
	$(IMGFS) list

imgfs-commit:
	$(IMGFS) commit

imgfs-clean:
	@rm -f imgfs_manifest.jsonl imgfs_ledger.jsonl
	@echo "imgfs manifest and ledger removed."

add:
ifndef FILE
	$(error FILE is not set. Usage: make add FILE=path/to/file)
endif
	$(IMGFS) add $(FILE)

verify-file:
ifndef FILE
	$(error FILE is not set. Usage: make verify-file FILE=path/to/file)
endif
	$(IMGFS) verify $(FILE)

# ── Clean ─────────────────────────────────────────────────────────────────────

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -rf dist build *.egg-info .pytest_cache .ruff_cache
