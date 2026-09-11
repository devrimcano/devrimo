"""Whose SAIS session the catalog is read through, and who decides.

A whole-term import is about eleven thousand page loads behind one student's
login. Until `catalog_source_metu_username` existed, which student was decided
by the calendar: every active account with Course Info connected was eligible
and the rotation picked one by the day of the year. It changed overnight, and
the person running the deployment could not tell whose account was carrying it
without reading the function.

That is worth a test on its own, because the cost of getting it wrong is not a
failed job - it is eleven thousand requests through a real person's account.
"""

from types import SimpleNamespace
from uuid import UUID

import pytest

from app.academic_catalog import worker

ONE = UUID("011210c1-0151-40b0-ad7a-4e3469f4a884")
TWO = UUID("42d7bf2d-cfff-4aff-8da8-5bb583dbaa66")
ORG = UUID("00000000-0000-0000-0000-000000000001")


def _secret(username: str):
    return SimpleNamespace(metu_username=username, has=lambda field: field == "metu_password")


@pytest.fixture
def two_connected_accounts(monkeypatch):
    """Two students with Course Info connected, as production has."""
    usernames = {ONE: "e272479", TWO: "e273802"}

    class Scalars:
        def __init__(self, values):
            self._values = values

        def all(self):
            return self._values

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def scalars(self, _statement):
            return Scalars(list(usernames))

    monkeypatch.setattr(worker, "SessionLocal", Session)
    monkeypatch.setattr(worker.campus_service, "users_with_tool", _async(list(usernames)))
    monkeypatch.setattr(worker.campus_service, "get_credential", _async_by_user(usernames))
    monkeypatch.setattr(worker, "secrets_for", lambda value: value)
    return usernames


def _async(value):
    async def call(*_args, **_kwargs):
        return value

    return call


def _async_by_user(usernames):
    async def call(_db, user_id):
        return _secret(usernames[user_id])

    return call


async def test_the_named_account_is_the_one_used(two_connected_accounts, monkeypatch):
    """Not whichever the date happens to land on."""
    monkeypatch.setattr(worker.get_settings(), "catalog_source_metu_username", "e273802")
    user_id, secret = await worker._account(ORG)
    assert secret.metu_username == "e273802"
    assert user_id == TWO


async def test_the_name_is_matched_without_case_or_padding(two_connected_accounts, monkeypatch):
    """It is typed into an env file by hand."""
    monkeypatch.setattr(worker.get_settings(), "catalog_source_metu_username", "  E273802 ")
    _user_id, secret = await worker._account(ORG)
    assert secret.metu_username == "e273802"


async def test_a_named_account_that_is_not_available_yields_nothing(two_connected_accounts, monkeypatch):
    """Never a silent substitution.

    Falling back to another student is exactly the surprise this setting exists
    to remove: someone names one account, that account stops being usable, and
    eleven thousand requests quietly move to a person who was never asked. The
    job reports no_eligible_source_account instead, where an admin can see it.
    """
    monkeypatch.setattr(worker.get_settings(), "catalog_source_metu_username", "e999999")
    assert await worker._account(ORG) is None


async def test_without_a_name_the_old_rotation_still_applies(two_connected_accounts, monkeypatch):
    """Deployments that never set this keep the behaviour they had."""
    monkeypatch.setattr(worker.get_settings(), "catalog_source_metu_username", "")
    _user_id, secret = await worker._account(ORG)
    assert secret.metu_username in {"e272479", "e273802"}
