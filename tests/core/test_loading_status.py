"""Progress uses the shared application emoji with a plain-text fallback."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.discord.application_emojis import ApplicationEmojiCatalog, loading_status


class LoadingStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_loading_emoji_uses_existing_provider(self):
        client = SimpleNamespace(fetch_application_emojis=AsyncMock())
        provider = SimpleNamespace(get=AsyncMock(return_value=ApplicationEmojiCatalog({
            "loading": "<a:loading:123>",
        })))
        with patch(
            "elbow_helper.discord.application_emojis.get_application_emoji_provider",
            return_value=provider,
        ) as get_provider:
            self.assertEqual(await loading_status(client, "Sending DM"),
                             "<a:loading:123> Sending DM")
        get_provider.assert_called_once_with(client)
        provider.get.assert_awaited_once_with(required_names=("loading",))

    async def test_missing_emoji_keeps_status_word_for_word(self):
        client = SimpleNamespace(fetch_application_emojis=AsyncMock())
        provider = SimpleNamespace(get=AsyncMock(return_value=ApplicationEmojiCatalog({})))
        with patch(
            "elbow_helper.discord.application_emojis.get_application_emoji_provider",
            return_value=provider,
        ):
            self.assertEqual(await loading_status(client, "Updating..."), "Updating...")

    async def test_unavailable_client_keeps_plain_status(self):
        self.assertEqual(await loading_status(None, "Sending DM"), "Sending DM")
