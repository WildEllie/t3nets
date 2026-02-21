# T3nets — DynamoDB Schema Reference

**Last Updated:** February 21, 2026

---

## Overview

T3nets uses two DynamoDB tables with PAY_PER_REQUEST billing:

1. **conversations** — Short-term conversation memory (30-day TTL)
2. **tenants** — Single-table design for tenants, users, and channel mappings

---

## Table: conversations

Stores active conversation history. Auto-expires after 30 days of inactivity.

### Key Schema

| Key | Pattern | Example |
|-----|---------|---------|
| PK (partition key) | `{tenant_id}` | `default` |
| SK (sort key) | `{conversation_id}` | `conv-abc-123` |

### Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `messages` | String (JSON) | Array of `{role, content}` message objects |
| `updated_at` | String (ISO 8601) | Last activity timestamp |
| `ttl` | Number (Unix epoch) | Auto-delete after 30 days from last update |

### Access Patterns

| Operation | Key Condition |
|-----------|--------------|
| Get conversation | PK = `{tenant_id}`, SK = `{conversation_id}` |
| Save turn | PK = `{tenant_id}`, SK = `{conversation_id}` (PutItem with updated messages) |
| Clear conversation | PK = `{tenant_id}`, SK = `{conversation_id}` (DeleteItem) |

### Message Format

```json
{
  "messages": [
    {"role": "user", "content": "what's the sprint status?"},
    {"role": "assistant", "content": "Here's your current sprint..."}
  ],
  "updated_at": "2026-02-21T14:30:00Z",
  "ttl": 1743004200
}
```

---

## Table: tenants

Single-table design storing tenants, users, and channel mappings. Uses item type prefixes in sort keys.

### Key Schema

| Key | Pattern |
|-----|---------|
| PK (partition key) | `TENANT#{tenant_id}` or `USER#{tenant_id}` |
| SK (sort key) | `META`, `USER#{user_id}`, or `CHANNEL#{type}#{id}` |

### GSI: channel-mapping

| Key | Pattern | Example |
|-----|---------|---------|
| gsi1pk | `CHANNEL#{type}#{id}` | `CHANNEL#teams#azure-bot-app-id` |
| gsi1sk | `TENANT#{tenant_id}` | `TENANT#outlocks` |

Used to resolve which tenant owns a channel identifier (from webhook payloads).

---

### Item Type: Tenant Metadata

| Field | Key | Example |
|-------|-----|---------|
| PK | `TENANT#{tenant_id}` | `TENANT#outlocks` |
| SK | `META` | `META` |

| Attribute | Type | Description |
|-----------|------|-------------|
| `tenant_id` | String | Unique tenant identifier |
| `name` | String | Display name (e.g., "Outlocks") |
| `status` | String | `active`, `suspended`, `onboarding` |
| `created_at` | String (ISO 8601) | Creation timestamp |
| `settings` | String (JSON) | `TenantSettings` serialized |

**Settings JSON:**
```json
{
  "ai_provider": "bedrock",
  "ai_model": "claude-sonnet-4-5-20250929",
  "system_prompt_override": "",
  "max_tokens_per_message": 4096,
  "enabled_channels": ["dashboard"],
  "enabled_skills": ["sprint_status"],
  "custom_skills": [],
  "messages_per_day": 1000,
  "max_conversation_history": 20
}
```

---

### Item Type: User

| Field | Key | Example |
|-------|-----|---------|
| PK | `TENANT#{tenant_id}` | `TENANT#outlocks` |
| SK | `USER#{user_id}` | `USER#admin` |

| Attribute | Type | Description |
|-----------|------|-------------|
| `user_id` | String | Unique within tenant |
| `email` | String | User email |
| `display_name` | String | Display name |
| `role` | String | `admin` or `member` |
| `channel_identities` | String (JSON) | `{"teams": "aad-id", "slack": "U12345"}` |

**Future expansion (no migration needed):**

| Attribute | Type | Description |
|-----------|------|-------------|
| `preferences` | String (JSON) | User-specific settings |
| `custom_properties` | String (JSON) | Tenant-defined custom fields |
| `memory_summary` | String | AI-generated user context summary |
| `memory_updated_at` | String (ISO 8601) | When memory was last refreshed |

---

### Item Type: Channel Mapping

| Field | Key | Example |
|-------|-----|---------|
| PK | `TENANT#{tenant_id}` | `TENANT#outlocks` |
| SK | `CHANNEL#{type}#{id}` | `CHANNEL#teams#azure-bot-app-id` |
| gsi1pk | `CHANNEL#{type}#{id}` | `CHANNEL#teams#azure-bot-app-id` |

Used for tenant resolution from channel webhooks:

```
Incoming Teams webhook → extract bot_app_id
→ Query GSI: gsi1pk = "CHANNEL#teams#azure-bot-app-id"
→ Returns TENANT#outlocks
→ Load tenant metadata
```

---

## Query Examples

### Get tenant by ID
```python
table.get_item(Key={
    'pk': f'TENANT#{tenant_id}',
    'sk': 'META'
})
```

### Resolve tenant from channel
```python
table.query(
    IndexName='channel-mapping',
    KeyConditionExpression='gsi1pk = :channel_key',
    ExpressionAttributeValues={
        ':channel_key': f'CHANNEL#{channel_type}#{channel_id}'
    }
)
```

### List all users in a tenant
```python
table.query(
    KeyConditionExpression='pk = :pk AND begins_with(sk, :prefix)',
    ExpressionAttributeValues={
        ':pk': f'TENANT#{tenant_id}',
        ':prefix': 'USER#'
    }
)
```

### Find user by email (scan with filter)
```python
table.query(
    KeyConditionExpression='pk = :pk AND begins_with(sk, :prefix)',
    FilterExpression='email = :email',
    ExpressionAttributeValues={
        ':pk': f'TENANT#{tenant_id}',
        ':prefix': 'USER#',
        ':email': email
    }
)
```

---

## Capacity & Costs

Both tables use **PAY_PER_REQUEST** (on-demand) billing:
- Read: $0.25 per million read request units
- Write: $1.25 per million write request units
- Storage: $0.25 per GB/month

For a prototype with <1000 messages/day, expect <$2/month for DynamoDB.

---

## Future: Separate Memory Items

For per-user conversation memory (Phase 5), consider adding a new item type:

| PK | SK | Attributes |
|----|----|-----------| 
| `TENANT#{id}` | `MEMORY#{user_id}#{session_id}` | `summary`, `key_facts`, `created_at` |

This keeps memory items in the same table, queryable by tenant + user prefix.
