MAKEFLAGS += --no-print-directory
NS ?= /dev/nvme0n1
CTRL ?= /dev/nvme0

.PHONY: help setup check test shell baseline workload after diff poll analyze clean

help:
	@echo "targets:"
	@echo "  setup      install qemu / nvme-cli / virtme-ng (uses sudo)"
	@echo "  check      report tool + emulation-support status"
	@echo "  test       run the explore.py unit tests (no device needed)"
	@echo "  shell      interactive shell in the emulated-device VM"
	@echo "  baseline   capture output/baseline.json in the VM"
	@echo "  workload   run a small write + trim workload in the VM"
	@echo "  after      capture output/after.json in the VM"
	@echo "  diff       diff baseline.json vs after.json"
	@echo "  poll       30s SMART poll -> output/telemetry.sqlite (+ .csv)"
	@echo "  analyze    pandas/numpy trend analysis over output/telemetry.csv"
	@echo "  clean      remove output/ and the backing file"

setup:
	./env/setup.sh

check:
	./env/setup.sh --check

test:
	python3 -m unittest discover -s tests -v

shell:
	./env/up.sh

baseline:
	./env/up.sh -- python3 explore.py snapshot -o output/baseline.json
	./env/up.sh -- python3 explore.py check output/baseline.json || true

workload:
	./env/up.sh -- scripts/workload.sh

after:
	./env/up.sh -- python3 explore.py snapshot -o output/after.json

diff:
	./env/up.sh -- python3 explore.py diff output/baseline.json output/after.json

poll:
	./env/up.sh -- python3 explore.py poll --interval 3 --count 10 \
		--database output/telemetry.sqlite --csv output/telemetry.csv

analyze:
	python3 analyze_telemetry.py output/telemetry.csv

clean:
	rm -rf output env/nvme-backing.raw
