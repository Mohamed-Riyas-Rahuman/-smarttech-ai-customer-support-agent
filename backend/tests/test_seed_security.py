"""Security checks for environment-configured seed credentials."""
import pytest

from app.db import seed


class _NoWriteSession:
    def __init__(self):
        self.write_count = 0

    def scalar(self, *_args, **_kwargs):
        return None

    def add(self, *_args, **_kwargs):
        self.write_count += 1

    def commit(self):
        self.write_count += 1

    def rollback(self):
        pass

    def close(self):
        pass


def test_seed_requires_admin_password_before_writing(monkeypatch):
    session = _NoWriteSession()
    monkeypatch.setattr(seed, "SessionLocal", lambda: session)
    monkeypatch.setattr(seed.settings, "seed_admin_password", "")
    monkeypatch.setattr(seed.settings, "seed_demo_users", False)

    with pytest.raises(RuntimeError, match="SEED_ADMIN_PASSWORD"):
        seed.seed_database()

    assert session.write_count == 0
