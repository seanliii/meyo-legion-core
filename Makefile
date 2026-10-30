PY ?= python3.12
VENV ?= .venv
STATE ?=

.PHONY: venv check run

venv:
	$(PY) -m venv "$(VENV)"

check:
	@test -n "$(STATE)" || (echo '请指定独立实例目录：make check STATE=/绝对路径/实例'; exit 2)
	"$(VENV)/bin/python" -I meyo.py check --state-dir "$(STATE)"

run:
	@test -n "$(STATE)" || (echo '请指定独立实例目录：make run STATE=/绝对路径/实例'; exit 2)
	"$(VENV)/bin/python" -I meyo.py serve --state-dir "$(STATE)"
