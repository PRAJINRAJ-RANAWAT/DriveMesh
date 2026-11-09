"""Safe mode exit rule (08-how-it-works §4.5, M8).

Rule: safe mode is entered when the workers start and whenever a health pass finds accounts
but none of them active/full/draining. It is left on the first health pass in which at least
one account is healthy (or there are no accounts). There is no timeout.
"""

from __future__ import annotations

from drivemesh.backends.base import TransientError
from drivemesh.config import Settings
from drivemesh.core.workers import WorkerSet


class _Down:
    def __init__(self, inner):
        self.inner, self.provider = inner, inner.provider

    def health(self):
        raise TransientError("simulated outage")

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_unused_safe_mode_timeout_setting_is_gone():
    """Regression for M8: the setting was declared but never read, which implied a timeout
    rule that did not exist."""
    assert "safe_mode_timeout" not in Settings.model_fields


def test_startup_leaves_safe_mode_once_an_account_is_healthy(dm, user, accounts):
    ws = WorkerSet(dm)
    dm.safe_mode.enter("startup")
    ws._health()
    assert not dm.safe_mode.active


def test_enters_when_every_account_is_down_and_leaves_on_recovery(dm, user, accounts):
    ws = WorkerSet(dm)
    for a in accounts:
        dm.accounts.wrap(a.id, _Down)
    # Transient failures keep the old state until `unreachable_after_failures` probes fail.
    for _ in range(dm.settings.unreachable_after_failures - 1):
        ws._health()
        assert not dm.safe_mode.active
    ws._health()
    assert dm.safe_mode.active and dm.safe_mode.reason == "no reachable storage accounts"
    dm.accounts.wrap(accounts[0].id, lambda inner: inner)  # one account recovers
    ws._health()
    assert not dm.safe_mode.active


def test_no_accounts_is_not_safe_mode(dm, user):
    ws = WorkerSet(dm)
    dm.safe_mode.enter("startup")
    ws._health()
    assert not dm.safe_mode.active
