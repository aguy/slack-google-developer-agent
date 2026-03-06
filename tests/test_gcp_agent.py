import unittest
from unittest.mock import AsyncMock, patch, MagicMock
import asyncio

from app.gcp_agent import query_agent

class MockPart:
    def __init__(self, text):
        self.text = text

class MockContent:
    def __init__(self, parts):
        self.parts = parts
        
class MockEvent:
    def __init__(self, author, partial, content=None):
        self.author = author
        self.partial = partial
        self.content = content

class TestGcpAgent(unittest.IsolatedAsyncioTestCase):

    @patch('app.gcp_agent._session_service', new_callable=AsyncMock)
    @patch('app.gcp_agent.get_runner', new_callable=AsyncMock)
    async def test_query_agent_success_with_none_text(self, mock_get_runner, mock_session_service):
        """Test that query_agent properly filters None values inside parts (the bug fix)."""
        mock_runner = MagicMock()
        mock_get_runner.return_value = mock_runner
        mock_session_service.get_session.return_value = True

        async def mock_run_async(*args, **kwargs):
            yield MockEvent("model", True, MockContent([MockPart("Hello ")]))
            # Simulate a chunk where the text attribute is explicitly None
            yield MockEvent("model", True, MockContent([MockPart(None)]))
            yield MockEvent("model", True, MockContent([MockPart("Hello World")]))
            # Final aggregated event
            yield MockEvent("model", False, MockContent([MockPart("Hello World")]))

        mock_runner.run_async = mock_run_async

        chunks = []
        async for chunk in query_agent("user1", "session1", "Say hi"):
            chunks.append(chunk)
            
        self.assertEqual(chunks, ["Hello ", "World"])

    @patch('app.gcp_agent._session_service', new_callable=AsyncMock)
    @patch('app.gcp_agent.get_runner', new_callable=AsyncMock)
    async def test_query_agent_fallback_no_deltas(self, mock_get_runner, mock_session_service):
        """Test that query_agent properly yields the full response if no partials are received."""
        mock_runner = MagicMock()
        mock_get_runner.return_value = mock_runner
        mock_session_service.get_session.return_value = True

        async def mock_run_async(*args, **kwargs):
            # No partials, just the final completed event
            yield MockEvent("model", False, MockContent([MockPart("Complete response")]))

        mock_runner.run_async = mock_run_async

        chunks = []
        async for chunk in query_agent("user1", "session1", "Say hi"):
            chunks.append(chunk)
            
        self.assertEqual(chunks, ["Complete response"])

    @patch('app.gcp_agent._session_service', new_callable=AsyncMock)
    @patch('app.gcp_agent.get_runner', new_callable=AsyncMock)
    async def test_query_agent_no_response(self, mock_get_runner, mock_session_service):
        """Test that query_agent returns a default message if the agent yields no text."""
        mock_runner = MagicMock()
        mock_get_runner.return_value = mock_runner
        mock_session_service.get_session.return_value = True

        async def mock_run_async(*args, **kwargs):
            yield MockEvent("model", False, MockContent([MockPart("")]))

        mock_runner.run_async = mock_run_async

        chunks = []
        async for chunk in query_agent("user1", "session1", "Say hi"):
            chunks.append(chunk)
            
        self.assertEqual(chunks, ["No response generated."])

    @patch('asyncio.sleep', new_callable=AsyncMock)
    @patch('app.gcp_agent._session_service', new_callable=AsyncMock)
    @patch('app.gcp_agent.get_runner', new_callable=AsyncMock)
    async def test_query_agent_retries_on_error(self, mock_get_runner, mock_session_service, mock_sleep):
        """Test that query_agent retries on failure and yields an error message if all retries fail."""
        mock_runner = MagicMock()
        
        async def failing_run_async(*args, **kwargs):
            raise ConnectionError("Network failure")
            yield  # To make it a generator
            
        mock_runner.run_async = failing_run_async
        mock_get_runner.return_value = mock_runner

        chunks = []
        async for chunk in query_agent("user1", "session1", "Say hi"):
            chunks.append(chunk)

        self.assertEqual(mock_get_runner.call_count, 6) # 3 attempts, 2 calls per attempt (1 initial + 1 fallback rebuild)
        self.assertEqual(chunks, ["⚠️ I'm having trouble connecting to my knowledge tools."])

if __name__ == '__main__':
    unittest.main()