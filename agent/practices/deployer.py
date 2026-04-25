"""
Practice deployer — AWS Lambda + EventBridge deployment for practice skills.

Each practice skill with a `lambda.zip` artifact gets a dedicated Lambda
function and EventBridge rule routing `skill.invoke` events to it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from agent.models.practice import PracticeDefinition

logger = logging.getLogger(__name__)


async def deploy_skill_lambdas(
    practice: PracticeDefinition,
    config: dict[str, Any],
) -> list[str]:
    """Deploy Lambda functions + EventBridge rules for each skill with a lambda.zip.

    Args:
        practice: The installed practice definition
        config: Lambda deployment config with keys:
            region, name_prefix, lambda_role_arn, eventbridge_bus_name,
            eventbridge_bus_arn, eventbridge_dlq_arn, sqs_results_queue_url,
            secrets_prefix, pending_requests_table, s3_bucket_name,
            dynamodb_tenants_table, subnet_ids, security_group_id

    Returns list of deployed skill names.
    """
    import boto3  # type: ignore[import-untyped]

    region = config["region"]
    prefix = config["name_prefix"]
    lambda_client = boto3.client("lambda", region_name=region)
    events_client = boto3.client("events", region_name=region)
    logs_client = boto3.client("logs", region_name=region)

    deployed = []
    practice_dir = Path(practice.base_path)

    for skill_name in practice.skills:
        lambda_zip_path = practice_dir / "skills" / skill_name / "lambda.zip"
        if not lambda_zip_path.exists():
            logger.warning(f"No lambda.zip for skill {skill_name}, skipping Lambda deploy")
            continue

        func_name = f"{prefix}-skill-{skill_name}"
        log_group = f"/aws/lambda/{func_name}"
        rule_name = f"{prefix}-skill-invoke-{skill_name}"
        zip_bytes = lambda_zip_path.read_bytes()

        logger.info(f"Deploying Lambda for skill: {skill_name} ({len(zip_bytes)} bytes)")

        try:
            logs_client.create_log_group(logGroupName=log_group)
            logs_client.put_retention_policy(logGroupName=log_group, retentionInDays=14)
        except logs_client.exceptions.ResourceAlreadyExistsException:
            pass

        env_vars = {
            "T3NETS_PLATFORM": "aws",
            "T3NETS_STAGE": config.get("stage", "dev"),
            "AWS_REGION_NAME": region,
            "SECRETS_PREFIX": config["secrets_prefix"],
            "SQS_RESULTS_QUEUE_URL": config["sqs_results_queue_url"],
            "PENDING_REQUESTS_TABLE": config["pending_requests_table"],
            "S3_BUCKET_NAME": config.get("s3_bucket_name", ""),
            "DYNAMODB_TENANTS_TABLE": config.get("dynamodb_tenants_table", ""),
        }
        vpc_config = {}
        if config.get("subnet_ids") and config.get("security_group_id"):
            vpc_config = {
                "SubnetIds": config["subnet_ids"],
                "SecurityGroupIds": [config["security_group_id"]],
            }

        try:
            lambda_client.get_function(FunctionName=func_name)
            lambda_client.update_function_code(
                FunctionName=func_name,
                ZipFile=zip_bytes,
            )
            logger.info(f"  Updated Lambda: {func_name}")
        except lambda_client.exceptions.ResourceNotFoundException:
            create_kwargs: dict[str, Any] = {
                "FunctionName": func_name,
                "Role": config["lambda_role_arn"],
                "Handler": "adapters.aws.lambda_handler.handler",
                "Runtime": "python3.12",
                "Timeout": 120,
                "MemorySize": 512,
                "Code": {"ZipFile": zip_bytes},
                "Environment": {"Variables": env_vars},
            }
            if vpc_config:
                create_kwargs["VpcConfig"] = vpc_config
            lambda_client.create_function(**create_kwargs)
            logger.info(f"  Created Lambda: {func_name}")

        func_info = lambda_client.get_function(FunctionName=func_name)
        lambda_arn = func_info["Configuration"]["FunctionArn"]

        event_pattern = json.dumps(
            {
                "source": ["agent.router"],
                "detail-type": ["skill.invoke"],
                "detail": {"skill_name": [skill_name]},
            }
        )
        events_client.put_rule(
            Name=rule_name,
            EventBusName=config["eventbridge_bus_name"],
            EventPattern=event_pattern,
            Description=f"Route {skill_name} skill invocations to Lambda",
        )
        # EventBridge rule ARN format: arn:aws:events:{region}:{account}:rule/{bus}/{rule}
        account_region = config["eventbridge_bus_arn"].split(":event-bus/")[0]
        rule_arn = f"{account_region}:rule/{config['eventbridge_bus_name']}/{rule_name}"

        events_client.put_targets(
            Rule=rule_name,
            EventBusName=config["eventbridge_bus_name"],
            Targets=[
                {
                    "Id": f"skill-{skill_name}",
                    "Arn": lambda_arn,
                    "RetryPolicy": {
                        "MaximumRetryAttempts": 2,
                        "MaximumEventAgeInSeconds": 300,
                    },
                    "DeadLetterConfig": {
                        "Arn": config["eventbridge_dlq_arn"],
                    },
                }
            ],
        )

        # Remove old permission first (may have stale SourceArn)
        try:
            lambda_client.remove_permission(
                FunctionName=func_name,
                StatementId="AllowEventBridgeInvoke",
            )
        except Exception:
            pass
        try:
            lambda_client.add_permission(
                FunctionName=func_name,
                StatementId="AllowEventBridgeInvoke",
                Action="lambda:InvokeFunction",
                Principal="events.amazonaws.com",
                SourceArn=rule_arn,
            )
        except lambda_client.exceptions.ResourceConflictException:
            pass

        deployed.append(skill_name)
        logger.info(f"  Deployed Lambda + EventBridge for: {skill_name}")

    return deployed


async def ensure_skill_lambdas(
    practices: dict[str, PracticeDefinition],
    config: dict[str, Any],
) -> int:
    """Check that Lambdas exist for all practice skills. Deploy if missing.

    Called at startup after restoring practices from S3.
    Returns count of deployed/fixed Lambdas.
    """
    import boto3

    lambda_client = boto3.client("lambda", region_name=config["region"])
    fixed = 0

    for practice in practices.values():
        if practice.built_in:
            continue  # Built-in practices are deployed by deploy.sh/Terraform

        for skill_name in practice.skills:
            func_name = f"{config['name_prefix']}-skill-{skill_name}"
            try:
                lambda_client.get_function(FunctionName=func_name)
            except Exception:
                logger.info(f"Lambda missing for {skill_name}, deploying...")
                deployed = await deploy_skill_lambdas(practice, config)
                fixed += len(deployed)
                break  # All skills in this practice deployed together

    return fixed
