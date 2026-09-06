"""Shared test setup for the crypto-trading-bot test suite.

Makes the strategy modules importable and, when freqtrade is not installed
on the host, installs a minimal stub of the pieces StarterStrategy imports
(``freqtrade.strategy.IStrategy``) so the deterministic strategy logic can be
unit-tested on the host with plain pandas. Inside the freqtrade Docker image
the real freqtrade is importable and the stub is skipped — the same tests
then validate against the true interface.
"""

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STRATEGIES_DIR = REPO_ROOT / "user_data" / "strategies"

for _path in (str(REPO_ROOT), str(STRATEGIES_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

try:  # real freqtrade (inside the Docker image) — nothing to stub
    import freqtrade  # noqa: F401
except ModuleNotFoundError:  # host run — stub the minimal surface
    _freqtrade_pkg = types.ModuleType("freqtrade")
    _freqtrade_pkg.__path__ = []  # mark as a package for import machinery
    _strategy_mod = types.ModuleType("freqtrade.strategy")

    class IStrategy:  # minimal stand-in: enough for logic-level unit tests
        def __init__(self, config=None):
            self.config = config or {}
            self.dp = None

    _strategy_mod.IStrategy = IStrategy
    _freqtrade_pkg.strategy = _strategy_mod
    sys.modules["freqtrade"] = _freqtrade_pkg
    sys.modules["freqtrade.strategy"] = _strategy_mod
