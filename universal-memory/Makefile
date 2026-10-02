#
# Universal Memory — build/test/install. No dependencies beyond python3.
#
#   make test        run the test suite
#   make install     install the CLI to ~/.local/bin (+ legacy umem alias)
#   make serve       run the MCP HTTP endpoint (continuous; port 8787)
#   make web         run the dashboard + JSON API on demand (port 8788)
#   make unit        install + enable the systemd user unit for the MCP endpoint
#   make clean       drop caches and build leftovers
#

PY      ?= python3
PREFIX  ?= $(HOME)/.local/bin
BIN     := $(PREFIX)/universal-memory
SRC     := src/universal_memory.py
TESTS   := tests/test_universal_memory.py
UNIT    := $(HOME)/.config/systemd/user/universal-memory-mcp.service

.PHONY: all test install serve web unit uninstall clean

all: test

test:
	$(PY) $(TESTS) -v

install: $(SRC)
	install -d $(PREFIX)
	install -m 0755 $(SRC) $(BIN)
	ln -sf $(BIN) $(PREFIX)/umem
	$(BIN) init || true
	@echo "installed $(BIN)"
	@echo "run:  $(BIN) whoami"

serve:
	$(BIN) serve

web:
	$(BIN) web

unit: install
	install -d $(dir $(UNIT))
	install -m 0644 packaging/universal-memory-mcp.service $(UNIT)
	systemctl --user daemon-reload
	systemctl --user enable --now universal-memory-mcp.service
	systemctl --user --no-pager status universal-memory-mcp.service

uninstall:
	rm -f $(BIN) $(PREFIX)/umem
	systemctl --user disable --now universal-memory-mcp.service 2>/dev/null || true
	rm -f $(UNIT)

clean:
	rm -rf src/__pycache__ tests/__pycache__ .pytest_cache
