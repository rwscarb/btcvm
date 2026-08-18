.PHONY: help install dev test lint fix run commit status clean completion
.PHONY: ott-status ott-list ott-commit ott-clean ott-repo-add ott-tag ott-push ott-snapshot ott-release add verify-file

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
	@echo "  make ott-status                  Show archive status"
	@echo "  make ott-list                    List archived files"
	@echo "  make ott-commit                  Commit Merkle root to ledger"
	@echo "  make ott-clean                   Remove flat manifest/ledger"
	@echo "  make ott-repo-add                Update repo record to HEAD"
	@echo "  make ott-snapshot                repo-add + commit root"
	@echo "  make ott-tag [OTT_NEXT_TAG=v1.x] Sign tag + record fingerprint"
	@echo "  make ott-push [OTT_NEXT_TAG=v1.x] Push commits + tag + commit root"
	@echo "  make ott-release [OTT_NEXT_TAG=v1.x] Full: tag + push + commit"
	@echo ""
	@echo "  make add FILE=path/to/file.jpg   Add file to archive"
	@echo "  make verify-file FILE=path       Verify file inclusion"
	@echo ""
	@echo "  Overrides: OTT_REPO=. OTT_NEXT_TAG=v1.2.3 OTT_KEY=DEADBEEF OTT_MSG=\"msg\""

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

# Configurable vars (override on command line)
OTT_REPO     ?= .
OTT_KEY      ?=
OTT_MSG      ?=
# OTT_NEXT_TAG: auto-increments patch; override: make ott-tag OTT_NEXT_TAG=v2.0.0
OTT_NEXT_TAG ?= _auto_

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

## Update archived repo record to current HEAD
ott-repo-add:
	$(OTT) repo add $(OTT_REPO)

## Create a signed git tag and record GPG fingerprint in ott
## Usage: make ott-tag [OTT_NEXT_TAG=v1.2.3] [OTT_KEY=DEADBEEF] [OTT_MSG="msg"]
ott-tag: ott-repo-add
	@TAG="$(OTT_NEXT_TAG)"; \
	 if [ "$$TAG" = "_auto_" ]; then \
	   PREV=$$(git -C $(OTT_REPO) describe --tags --abbrev=0 2>/dev/null || echo ""); \
	   if echo "$$PREV" | grep -qE '^v?[0-9]+\.[0-9]+\.[0-9]+$$'; then \
	     TAG=$$(echo "$$PREV" | python3 -c "import sys,re; m=re.match(r'v?(\d+)\.(\d+)\.(\d+)',sys.stdin.read().strip()); print('v{}.{}.{}'.format(m[1],m[2],int(m[3])+1))"); \
	   else \
	     TAG="v0.0.1"; \
	   fi; \
	 fi; \
	 echo "  Tagging as $$TAG"; \
	 $(OTT) repo tag $(OTT_REPO) $$TAG $(OTT_KEY) $(if $(OTT_MSG),"$(OTT_MSG)"); \
	 echo ""; \
	 echo "  Done. Run: make ott-push OTT_NEXT_TAG=$$TAG"

## Push commits + signed tag to origin, then commit ott Merkle root
## Usage: make ott-push [OTT_NEXT_TAG=v1.2.3]
ott-push: ott-repo-add
	@echo "  Pushing commits to origin..."
	git -C $(OTT_REPO) push origin HEAD
	@echo "  Pushing tag $(OTT_NEXT_TAG)..."
	git -C $(OTT_REPO) push origin $(OTT_NEXT_TAG)
	@echo "  Committing ott Merkle root to ledger..."
	$(OTT) commit

## Snapshot: update repo record + commit Merkle root (no tag, no push)
ott-snapshot: ott-repo-add ott-commit
	@echo "  Snapshot complete."

## Full release: tag → push → commit root
## Usage: make ott-release [OTT_NEXT_TAG=v1.2.3] [OTT_KEY=DEADBEEF] [OTT_MSG="release"]
ott-release: ott-tag ott-push
	@echo ""
	@echo "✅ Release $(OTT_NEXT_TAG) complete."
	@echo "   To anchor on-chain: python broadcast.py <commitment above>"

# ── Clean ─────────────────────────────────────────────────────────────────────

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -rf dist build *.egg-info .pytest_cache .ruff_cache
