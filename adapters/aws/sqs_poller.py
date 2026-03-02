"""
SQS Poller — Background thread that long-polls skill results from SQS.

The Lambda skill executor writes results to SQS after completing.
This poller runs inside the ECS router, picks up results, resolves the
pending request, and routes the response to the appropriate channel
(SSE for dashboard, Bot Framework for Teams, Telegram API, etc.).

Design:
    - Long-polling with WaitTimeSeconds=20 (near-zero cost when idle)
    - Processes messages one at a time (visibility_timeout=30s buffer)
    - Deletes message only after successful routing
    - Failures: message returns to queue, hits DLQ after 3 attempts

Thread model:
    - Single daemon thread, started from server init()
    - Shares sse_manager, ai provider, memory store via closure
    - Safe because SSEConnectionManager is thread-safe (internal lock)
"""

import json
import logging
import threading
import time

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from typing import Any, Callable

logger = logging.getLogger(__name__)


class SQSResultPoller:
    """
    Long-polls SQS for skill results and routes them to channels.

    The callback receives the parsed SQS message body (dict) and is
    responsible for routing the result to the correct channel.
    """

    def __init__(
        self,
        queue_url: str,
        callback: Callable[[dict[str, Any]], None],
        region: str = "us-east-1",
        wait_time_seconds: int = 20,
        max_messages: int = 1,
    ):
        self.queue_url = queue_url
        self.callback = callback  # fn(message_body: dict) -> None
        self.region = region
        self.wait_time_seconds = wait_time_seconds
        self.max_messages = max_messages
        self._running = False
        self._thread: threading.Thread | None = None
        self._client = boto3.client("sqs", region_name=region)

    def start(self) -> threading.Thread:
        """Start the poller in a daemon thread. Returns the thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="sqs-result-poller",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"SQSResultPoller: started (queue={self.queue_url[-40:]})")
        return self._thread

    def stop(self) -> None:
        """Signal the poller to stop. Thread will exit after current poll."""
        self._running = False
        logger.info("SQSResultPoller: stop requested")

    def _poll_loop(self) -> None:
        """Main polling loop. Runs until stop() is called."""
        consecutive_errors = 0

        while self._running:
            try:
                response = self._client.receive_message(
                    QueueUrl=self.queue_url,
                    MaxNumberOfMessages=self.max_messages,
                    WaitTimeSeconds=self.wait_time_seconds,
                    MessageAttributeNames=["All"],
                )
                consecutive_errors = 0

                messages = response.get("Messages", [])
                if not messages:
                    continue  # Empty poll — normal, just loop again

                for msg in messages:
                    self._process_message(msg)

            except ClientError as e:
                consecutive_errors += 1
                backoff = min(consecutive_errors * 2, 30)
                logger.error(
                    f"SQSResultPoller: AWS error (attempt {consecutive_errors}): {e}. "
                    f"Retrying in {backoff}s"
                )
                time.sleep(backoff)
            except Exception as e:
                consecutive_errors += 1
                backoff = min(consecutive_errors * 2, 30)
                logger.exception(
                    f"SQSResultPoller: unexpected error (attempt {consecutive_errors}): {e}. "
                    f"Retrying in {backoff}s"
                )
                time.sleep(backoff)

        logger.info("SQSResultPoller: stopped")

    def _process_message(self, msg: dict[str, Any]) -> None:
        """Parse and route a single SQS message, then delete it."""
        receipt_handle = msg["ReceiptHandle"]
        body_str = msg.get("Body", "{}")

        try:
            body = json.loads(body_str)
        except json.JSONDecodeError:
            logger.error(f"SQSResultPoller: invalid JSON in message, deleting: {body_str[:200]}")
            self._delete_message(receipt_handle)
            return

        request_id = body.get("request_id", "?")
        skill_name = body.get("skill_name", "?")
        logger.info(
            f"SQSResultPoller: received result for "
            f"skill={skill_name}, request={request_id[:8]}"
        )

        try:
            self.callback(body)
            logger.info(f"SQSResultPoller: routed result for request {request_id[:8]}")
        except Exception as e:
            logger.exception(
                f"SQSResultPoller: callback failed for request {request_id[:8]}: {e}. "
                "Message will return to queue."
            )
            # Don't delete — let it return to queue for retry
            return

        # Success — remove from queue
        self._delete_message(receipt_handle)

    def _delete_message(self, receipt_handle: str) -> None:
        """Delete a processed message from SQS."""
        try:
            self._client.delete_message(
                QueueUrl=self.queue_url,
                ReceiptHandle=receipt_handle,
            )
        except ClientError as e:
            logger.error(f"SQSResultPoller: failed to delete message: {e}")
