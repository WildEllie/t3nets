"""
Practice page publishing for AWS — uploads page HTML files to the static
S3 bucket under `p/{practice_name}/{file}` and invalidates the matching
CloudFront paths so the new version is served immediately.

Built-in practice pages are uploaded by `deploy.sh`; this module handles
*uploaded* practices (installed at runtime via POST /api/practices/upload).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import boto3  # type: ignore[import-untyped]

from agent.models.practice import PracticeDefinition

logger = logging.getLogger(__name__)


def publish_practice_pages(
    practice: PracticeDefinition,
    s3_bucket: str,
    cloudfront_distribution_id: str,
    region: str = "us-east-1",
) -> int:
    """Upload practice page files to S3 and invalidate CloudFront `/p/{name}/*`.

    Returns the number of page files uploaded.

    No-op if either ``s3_bucket`` or ``cloudfront_distribution_id`` is empty
    (e.g. local dev or a stack without CDN wiring) — the caller can still
    serve pages via the API-Gateway fallback.
    """
    if not s3_bucket:
        logger.info("publish_practice_pages: no S3 bucket configured, skipping")
        return 0

    practice_dir = Path(practice.base_path)
    s3 = boto3.client("s3", region_name=region)

    uploaded = 0
    for page in practice.pages:
        src = practice_dir / page.file
        if not src.exists():
            logger.warning(f"publish_practice_pages: missing {src}, skipping")
            continue
        key = f"p/{practice.name}/{page.file}"
        s3.put_object(
            Bucket=s3_bucket,
            Key=key,
            Body=src.read_bytes(),
            ContentType="text/html; charset=utf-8",
        )
        uploaded += 1
        logger.info(f"publish_practice_pages: s3://{s3_bucket}/{key}")

    if uploaded and cloudfront_distribution_id:
        cf = boto3.client("cloudfront", region_name=region)
        # Invalidate only this practice's path to avoid blowing the global cache.
        invalidation: dict[str, Any] = cf.create_invalidation(
            DistributionId=cloudfront_distribution_id,
            InvalidationBatch={
                "Paths": {
                    "Quantity": 1,
                    "Items": [f"/p/{practice.name}/*"],
                },
                "CallerReference": f"practice-{practice.name}-{practice.version}",
            },
        )
        inv_id = invalidation.get("Invalidation", {}).get("Id", "?")
        logger.info(
            f"publish_practice_pages: CloudFront invalidation {inv_id} for /p/{practice.name}/*"
        )

    return uploaded
