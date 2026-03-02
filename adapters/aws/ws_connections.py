"""
WebSocket Connection Manager — API Gateway WebSocket push via Management API.

DynamoDB-backed registry of WebSocket connection IDs. Any ECS task can read
and write the shared connection table, enabling horizontal scaling — async
skill results are delivered regardless of which task handled the $connect.

Schema (table: {project}-{environment}-ws-connections):
  pk       = connection_id   (hash key)
  user_key = email/sub       (GSI hash key — fan-out by user)
  ttl      = Unix epoch      (now + 7200s; DynamoDB auto-deletes stale rows)

GSI: user-connections-index on user_key — Query by user to get all active
connection IDs.

Part of the AWS adapter layer — uses boto3 for DynamoDB and Management API.
"""

import json
import logging
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_WS_CONNECTION_TTL = 7200  # 2 hours — API Gateway's max connection duration
_GSI_NAME = "user-connections-index"


class WebSocketConnectionManager:
    """DynamoDB-backed registry of WebSocket connection IDs.

    Mirrors SSEConnectionManager.send_event() so AsyncResultRouter doesn't
    need to know which transport is in use.
    """

    def __init__(self, management_endpoint: str, table_name: str, region: str) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=region)
        self._table = dynamodb.Table(table_name)
        self._mgmt_client = boto3.client(
            "apigatewaymanagementapi",
            endpoint_url=management_endpoint,
            region_name=region,
        )

    def register(self, user_key: str, connection_id: str) -> None:
        """Register a WebSocket connection for a user."""
        ttl = int(time.time()) + _WS_CONNECTION_TTL
        self._table.put_item(
            Item={
                "pk": connection_id,
                "user_key": user_key,
                "ttl": ttl,
            }
        )
        logger.info(f"WS: registered {connection_id[:12]} for {user_key}")

    def unregister_by_connection_id(self, connection_id: str) -> None:
        """Remove a WebSocket connection by its connection ID."""
        self._table.delete_item(Key={"pk": connection_id})
        logger.info(f"WS: unregistered {connection_id[:12]}")

    def get_connections(self, user_key: str) -> list[str]:
        """Get all active connection IDs for a user via GSI query."""
        response = self._table.query(
            IndexName=_GSI_NAME,
            KeyConditionExpression="user_key = :uk",
            ExpressionAttributeValues={":uk": user_key},
        )
        return [item["pk"] for item in response.get("Items", [])]

    @property
    def connection_count(self) -> int:
        """Count active connections via DynamoDB Scan (health check only — not a hot path)."""
        try:
            resp = self._table.scan(Select="COUNT")
            return resp.get("Count", 0)
        except Exception:
            return 0

    def send_event(self, user_key: str, event_type: str, data: dict) -> int:
        """Push event via ApiGatewayManagementApi. Returns delivery count.

        Matches SSEConnectionManager.send_event() signature so result_router
        doesn't need to know which transport is in use.

        Stale connections (GoneException) are deleted from DynamoDB immediately.
        """
        connections = self.get_connections(user_key)
        if not connections:
            logger.debug(f"WS: no connections for {user_key}")
            return 0

        payload = json.dumps({"event": event_type, **data}).encode()
        sent = 0

        for conn_id in connections:
            try:
                self._mgmt_client.post_to_connection(
                    ConnectionId=conn_id,
                    Data=payload,
                )
                sent += 1
            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "")
                if error_code == "GoneException":
                    logger.debug(f"WS: stale connection {conn_id[:12]}, removing")
                    self.unregister_by_connection_id(conn_id)
                else:
                    logger.error(f"WS: post_to_connection failed for {conn_id[:12]}: {e}")

        return sent
