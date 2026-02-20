# T3nets

An open-source, multi-tenant AI agent platform. Teams connect their tools
(Jira, GitHub, Google Workspace, etc.) and talk to an AI assistant through
the channels they already use.

## How It Works

```
You (Teams/Slack/WhatsApp/Dashboard) → T3nets → Claude AI → Your Tools → Answer
```

T3nets is the layer between your team's communication channels and their
productivity tools. Instead of switching between Jira, email, and calendars,
you ask a question and get an answer.

**Example:**
> "What's the sprint status?"
>
> 🏃 NOVA S12E4 — "Finish Lynx"
> 41% done, 5 days left. 2 blocked items. Risk: HIGH.
> Suggestion: Descope the test tickets and focus on getting blocked items through.

## Architecture

- **Cloud-agnostic core** — business logic has zero cloud imports
- **Pluggable channels** — Teams, Slack, WhatsApp, SMS, Voice, Dashboard, API
- **Pluggable skills** — add new capabilities without touching the router
- **Multi-tenant** — shared compute, isolated data
- **Serverless** — AWS reference implementation (ECS Fargate + Lambda + EventBridge)

See [docs/architecture.md](docs/architecture.md) for the full design.

## Quick Start (Local Development)

```bash
# Clone
git clone https://github.com/outlocks/t3nets.git
cd t3nets

# Set up Python environment
python3 -m venv venv
source venv/bin/activate
pip install -e ".[local,dev]"

# Configure
cp .env.example .env
# Edit .env with your Anthropic API key and Jira credentials

# Run
python -m adapters.local.dev_server
```

Open http://localhost:8080 to chat with your agent.

## Deploy on AWS

```bash
cd infra/aws
terraform init
terraform plan -var-file=environments/dev.tfvars
terraform apply -var-file=environments/dev.tfvars
```

See [docs/deployment.md](docs/deployment.md) for details.

## Extend

### Add a Skill

Create `agent/skills/my_skill/skill.yaml` and `worker.py`:

```yaml
# skill.yaml
name: my_skill
description: What this skill does
requires_integration: some_service
parameters:
  type: object
  properties:
    action:
      type: string
```

```python
# worker.py
def execute(params: dict, secrets: dict) -> dict:
    # Your business logic here
    return {"result": "..."}
```

See [docs/adding-a-skill.md](docs/adding-a-skill.md).

### Add a Channel

Implement `ChannelAdapter` (5 methods). See [docs/adding-a-channel.md](docs/adding-a-channel.md).

### Add a Cloud Provider

Implement 5 interfaces (AIProvider, ConversationStore, EventBus, SecretsProvider, BlobStore).
See [docs/adding-a-cloud.md](docs/adding-a-cloud.md).

## Project Structure

```
t3nets/
├── agent/                    # Portable application layer
│   ├── router/               # The brain
│   ├── skills/               # Skill definitions + workers
│   ├── channels/             # Channel adapters
│   ├── memory/               # Conversation management
│   ├── interfaces/           # Cloud-agnostic contracts
│   └── models/               # Shared data models
├── adapters/                 # Cloud-specific implementations
│   ├── aws/                  # AWS (reference implementation)
│   └── local/                # Local development
├── dashboard/                # Admin UI (React)
├── infra/                    # Terraform
└── docs/                     # Documentation
```

## Contributing

Contributions welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT
