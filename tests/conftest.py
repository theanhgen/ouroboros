"""Shared test fixtures.

The important one is the storage guard: OuroborosStorage() with no arguments
resolves to <repo>/config/ouroboros.db, which on a deployment is live agent
state. A test that constructs one without a path would read and write the real
database -- two stray improvement records got into it exactly that way while
this suite was being written.
"""

import pytest

from ouroboros import storage as storage_module


@pytest.fixture(autouse=True)
def _never_touch_the_real_database(tmp_path, monkeypatch):
    """Redirect the default storage path into the test's tmp_path."""
    real_init = storage_module.OuroborosStorage.__init__

    def guarded_init(self, db_path=None):
        if db_path is None:
            db_path = tmp_path / "config" / "ouroboros.db"
        real_init(self, db_path=db_path)

    monkeypatch.setattr(storage_module.OuroborosStorage, "__init__", guarded_init)
