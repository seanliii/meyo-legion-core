# 军团服务 · 一键命令
PY ?= python3.12
PORT ?= 8000

.PHONY: help run test test-fast check check-files verify-product verify-release install clean

help:
	@echo "军团服务 (legion-service) 命令:"
	@echo "  make run          启动服务 (http://127.0.0.1:$(PORT))"
	@echo "  make test         完整测试(含真调 harness·较慢)"
	@echo "  make test-fast    快速测试(内核+接口·跳过真调)"
	@echo "  make check        日常自适应快验收(按 diff 跑相关合同·结果按内容缓存)"
	@echo "  make check-files FILES='...' 只验本次会话文件，不受并行会话污染"
	@echo "  make verify-product PRODUCT=feature:... LEVELS=L0,L1,L2,L3,L4 FILES='...' 分级机器验收+产品门"
	@echo "  make verify-release PRODUCT=feature:... ZONES=all MOTIVATION='...' 仅复用当前机器证据+骨架门"
	@echo "  make install      装依赖(搜索工具等)"

run:
	$(PY) server.py --port $(PORT)

test:
	$(PY) tests/test_legion_service.py

test-fast:
	FAST=1 $(PY) tests/test_legion_service.py

check:
	@$(PY) harness_selfcheck.py --run-tests

check-files:
	@test -n "$(FILES)" || (echo "用法: make check-files FILES='release/legion-service/verification_reviews.py ...'" && exit 2)
	@$(PY) harness_selfcheck.py --run-tests --files $(FILES)

verify-product:
	@test -n "$(PRODUCT)" || (echo "必须指定 PRODUCT=feature:<功能名>" && exit 2)
	@$(PY) harness_selfcheck.py --run-tests --quality-levels "$(or $(LEVELS),L0,L1,L2,L3,L4)" $(if $(LIVE),--live-harnesses "$(LIVE)",) --product-target "$(PRODUCT)" --files $(FILES) >/dev/null || (echo "产品或机器发布门未通过：请到开发验收中心按阻塞项修复并复验" && exit 2)

verify-release:
	@test -n "$(PRODUCT)" || (echo "必须指定 PRODUCT=feature:<功能名>，并先完成 make verify-product 或界面机器复验" && exit 2)
	@$(PY) -c 'import verification_workflow as w,sys; d=w.release_decision("$(PRODUCT)"); print(d); sys.exit(0 if d.get("release_ready") else 1)' || (echo "当前提交的产品/机器发布门未通过" && exit 2)
	@case "$(ZONES)" in red) flag=--verify-red;; yellow) flag=--verify-yellow;; all|"") flag=--verify-all;; *) echo "ZONES 只能是 red/yellow/all"; exit 2;; esac; \
	$(PY) tests/test_skeleton_guard.py $$flag --motivation "$(MOTIVATION)"

install:
	$(PY) -m pip install -r requirements.txt

clean:
	find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true
