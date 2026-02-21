"""
AWS Tenant Store — DynamoDB.

Schema (single-table design):
  Tenants:
    pk: TENANT#{tenant_id}    sk: META
  Users:
    pk: TENANT#{tenant_id}    sk: USER#{user_id}
  Channel mappings (GSI):
    gsi1pk: CHANNEL#{channel_type}#{channel_specific_id}
"""

import json
import boto3
from typing import Optional

from agent.interfaces.tenant_store import TenantStore, TenantNotFound, UserNotFound
from agent.models.tenant import Tenant, TenantSettings, TenantUser


class DynamoDBTenantStore(TenantStore):
    """DynamoDB-backed tenant store (single-table design)."""

    def __init__(self, table_name: str, region: str = "us-east-1"):
        self.table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    # --- Tenant operations ---

    async def get_tenant(self, tenant_id: str) -> Tenant:
        response = self.table.get_item(
            Key={"pk": f"TENANT#{tenant_id}", "sk": "META"},
        )
        item = response.get("Item")
        if not item:
            raise TenantNotFound(f"Tenant '{tenant_id}' not found")
        return self._item_to_tenant(item)

    async def create_tenant(self, tenant: Tenant) -> None:
        self.table.put_item(
            Item=self._tenant_to_item(tenant),
            ConditionExpression="attribute_not_exists(pk)",
        )

    async def update_tenant(self, tenant: Tenant) -> None:
        self.table.put_item(Item=self._tenant_to_item(tenant))

    async def list_tenants(self) -> list[Tenant]:
        response = self.table.query(
            KeyConditionExpression="begins_with(pk, :prefix) AND sk = :meta",
            ExpressionAttributeValues={":prefix": "TENANT#", ":meta": "META"},
        )
        return [self._item_to_tenant(item) for item in response.get("Items", [])]

    # --- Channel mapping ---

    async def get_by_channel_id(self, channel_type: str, channel_specific_id: str) -> Tenant:
        gsi_key = f"CHANNEL#{channel_type}#{channel_specific_id}"

        response = self.table.query(
            IndexName="channel-mapping",
            KeyConditionExpression="gsi1pk = :gsi",
            ExpressionAttributeValues={":gsi": gsi_key},
        )

        items = response.get("Items", [])
        if not items:
            raise TenantNotFound(f"No tenant mapped to {channel_type}:{channel_specific_id}")

        # The item's pk contains the tenant_id
        tenant_id = items[0]["pk"].replace("TENANT#", "")
        return await self.get_tenant(tenant_id)

    async def set_channel_mapping(self, tenant_id: str, channel_type: str, channel_specific_id: str) -> None:
        gsi_key = f"CHANNEL#{channel_type}#{channel_specific_id}"

        self.table.put_item(
            Item={
                "pk": f"TENANT#{tenant_id}",
                "sk": f"CHANNEL#{channel_type}#{channel_specific_id}",
                "gsi1pk": gsi_key,
                "channel_type": channel_type,
                "channel_specific_id": channel_specific_id,
            }
        )

    # --- User operations ---

    async def get_user(self, tenant_id: str, user_id: str) -> TenantUser:
        response = self.table.get_item(
            Key={"pk": f"TENANT#{tenant_id}", "sk": f"USER#{user_id}"},
        )
        item = response.get("Item")
        if not item:
            raise UserNotFound(f"User '{user_id}' not found in tenant '{tenant_id}'")
        return self._item_to_user(item)

    async def get_user_by_email(self, tenant_id: str, email: str) -> Optional[TenantUser]:
        # Scan within tenant partition for email match
        response = self.table.query(
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            FilterExpression="email = :email",
            ExpressionAttributeValues={
                ":pk": f"TENANT#{tenant_id}",
                ":prefix": "USER#",
                ":email": email,
            },
        )
        items = response.get("Items", [])
        return self._item_to_user(items[0]) if items else None

    async def get_user_by_channel_identity(
        self, tenant_id: str, channel_type: str, channel_user_id: str,
    ) -> Optional[TenantUser]:
        # Query all users and filter by channel identity
        response = self.table.query(
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": f"TENANT#{tenant_id}",
                ":prefix": "USER#",
            },
        )

        for item in response.get("Items", []):
            identities = json.loads(item.get("channel_identities", "{}"))
            if identities.get(channel_type) == channel_user_id:
                return self._item_to_user(item)

        return None

    async def create_user(self, user: TenantUser) -> None:
        self.table.put_item(Item=self._user_to_item(user))

    async def update_user(self, user: TenantUser) -> None:
        self.table.put_item(Item=self._user_to_item(user))

    async def delete_user(self, tenant_id: str, user_id: str) -> None:
        self.table.delete_item(
            Key={"pk": f"TENANT#{tenant_id}", "sk": f"USER#{user_id}"},
        )

    async def list_users(self, tenant_id: str) -> list[TenantUser]:
        response = self.table.query(
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": f"TENANT#{tenant_id}",
                ":prefix": "USER#",
            },
        )
        return [self._item_to_user(item) for item in response.get("Items", [])]

    # --- Helpers ---

    def _tenant_to_item(self, tenant: Tenant) -> dict:
        return {
            "pk": f"TENANT#{tenant.tenant_id}",
            "sk": "META",
            "tenant_id": tenant.tenant_id,
            "name": tenant.name,
            "status": tenant.status,
            "created_at": tenant.created_at,
            "settings": json.dumps(tenant.settings.__dict__),
        }

    def _item_to_tenant(self, item: dict) -> Tenant:
        settings_dict = json.loads(item.get("settings", "{}"))
        return Tenant(
            tenant_id=item["tenant_id"],
            name=item["name"],
            status=item.get("status", "active"),
            created_at=item.get("created_at", ""),
            settings=TenantSettings(**settings_dict),
        )

    def _user_to_item(self, user: TenantUser) -> dict:
        return {
            "pk": f"TENANT#{user.tenant_id}",
            "sk": f"USER#{user.user_id}",
            "user_id": user.user_id,
            "tenant_id": user.tenant_id,
            "email": user.email,
            "display_name": user.display_name,
            "role": user.role,
            "channel_identities": json.dumps(user.channel_identities),
        }

    def _item_to_user(self, item: dict) -> TenantUser:
        return TenantUser(
            user_id=item["user_id"],
            tenant_id=item["tenant_id"],
            email=item["email"],
            display_name=item["display_name"],
            role=item.get("role", "member"),
            channel_identities=json.loads(item.get("channel_identities", "{}")),
        )
