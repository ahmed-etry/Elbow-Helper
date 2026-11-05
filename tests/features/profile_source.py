"""The collectors' profile path over a test Clash client, for feature tests."""

from types import SimpleNamespace

from elbow_helper.data.collectors import ProfileDemand


def profile_demand(client):
    """Profiles asked through the collectors; the closing owner keeps them out of any record."""
    return ProfileDemand(SimpleNamespace(clash_client=client, clans=(), _closing=True))
