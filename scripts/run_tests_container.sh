#!/bin/sh
# Run the pytest suite inside the freqtrade image — real freqtrade, no stub.
#
# Why the wrapper exists: the stable image ships WITHOUT pytest, and
# freqtrade's own /freqtrade/pyproject.toml injects pytest-xdist addopts
# that break a plain run of an unrelated suite. The -o addopts='' clears
# them. The container is ephemeral (--rm) and nothing is written back
# except read-only mounts.
set -eu
cd "$(dirname "$0")/.."
exec docker compose run --rm --entrypoint sh freqtrade \
  -c "pip install -q pytest && python3 -m pytest /freqtrade/tests/ -q -o addopts=''"
