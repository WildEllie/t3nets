"""
AWS S3 BlobStore — S3-backed object storage.

For production (ECS Fargate). Stores objects under {tenant_id}/{key} in an S3 bucket.
"""

import json
import logging
from typing import Any

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from agent.interfaces.blob_store import BlobNotFound, BlobStore

logger = logging.getLogger(__name__)


class S3BlobStore(BlobStore):
    """S3-backed blob store for production."""

    def __init__(self, bucket_name: str, region: str = "us-east-1"):
        self.bucket = bucket_name
        self.s3 = boto3.client("s3", region_name=region)

    def _key(self, tenant_id: str, key: str) -> str:
        """Build S3 object key from tenant and key."""
        safe_key = key.replace("..", "").lstrip("/")
        return f"{tenant_id}/{safe_key}"

    async def get(self, tenant_id: str, key: str) -> bytes:
        try:
            resp = self.s3.get_object(Bucket=self.bucket, Key=self._key(tenant_id, key))
            return resp["Body"].read()
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                raise BlobNotFound(f"{tenant_id}/{key}") from e
            raise

    async def get_json(self, tenant_id: str, key: str) -> dict[str, Any]:
        data = await self.get(tenant_id, key)
        return dict(json.loads(data.decode()))

    async def put(self, tenant_id: str, key: str, data: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=self._key(tenant_id, key), Body=data)

    async def put_json(self, tenant_id: str, key: str, data: dict[str, Any]) -> None:
        await self.put(tenant_id, key, json.dumps(data, indent=2).encode())

    async def delete(self, tenant_id: str, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=self._key(tenant_id, key))

    async def list_keys(self, tenant_id: str, prefix: str = "") -> list[str]:
        full_prefix = self._key(tenant_id, prefix)
        results: list[str] = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                # Strip tenant_id/ prefix to return relative keys
                rel_key = obj["Key"][len(tenant_id) + 1:]
                results.append(rel_key)
        return sorted(results)
