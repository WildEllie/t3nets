"""
t3nets-sdk testing utilities.

In-memory `Mock*` doubles for the t3nets interfaces, plus a builder for
`RequestContext`. Use these in practice repos to test skill workers
without standing up real cloud services.

Example:

    from t3nets_sdk.testing import (
        MockBlobStore,
        MockSecretsProvider,
        make_test_context,
    )

    async def test_my_skill():
        ctx = make_test_context(tenant_id="acme")
        secrets = MockSecretsProvider({"jira": {"url": "https://x", "token": "y"}})
        # ... call the worker, assert on results

Naming convention: all in-memory doubles are `Mock<Interface>`, never `Fake*`.
"""

from t3nets_sdk.testing.context import make_test_context
from t3nets_sdk.testing.mock_blob_store import MockBlobStore
from t3nets_sdk.testing.mock_conversation_store import MockConversationStore
from t3nets_sdk.testing.mock_event_bus import MockEventBus, PublishedEvent
from t3nets_sdk.testing.mock_secrets_provider import MockSecretsProvider

__all__ = [
    "MockBlobStore",
    "MockConversationStore",
    "MockEventBus",
    "MockSecretsProvider",
    "PublishedEvent",
    "make_test_context",
]
