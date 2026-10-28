# Shortcuts only. Every target is a plain docker compose command (see README).
RUN = docker compose run --rm --build pipeline

.PHONY: up demo load simulate dedup check report test fail-demo break-demo merge-source reset down

up:           ; docker compose up -d --wait
demo:         ; $(RUN) demo
load:         ; $(RUN) load
simulate:     ; $(RUN) simulate
dedup:        ; $(RUN) dedup
check:        ; $(RUN) check
report:       ; $(RUN) report
test:         ; $(RUN) test
merge-source: ; $(RUN) merge-source
fail-demo:
	$(RUN) simulate
	-$(RUN) load --fail-at cards
	-$(RUN) check
	$(RUN) load
	$(RUN) dedup
	$(RUN) check
break-demo:
	$(RUN) break --scenario lose-rows
	-$(RUN) check
	$(RUN) load --full-refresh transactions
	$(RUN) check
reset:        ; $(RUN) reset --yes
down:         ; docker compose down -v
