"""
`t3nets practice init NAME` — scaffold a new practice repository.

Writes a minimal, valid practice layout: `practice.yaml`, one example skill
with `skill.yaml` + `worker.py`, a placeholder test, and a short README.
The scaffold is intentionally tiny — enough to pass `t3nets practice
validate` and `t3nets practice package` with no further edits, so new
authors have a working baseline to grow from.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

_PRACTICE_YAML = """\
name: {name}
display_name: "{display}"
description: "A t3nets practice — describe what it does here."
version: "0.1.0"
icon: ""

integrations: []

skills:
  - example

pages: []
"""

_SKILL_YAML = """\
name: example
description: >
  Example skill — replace this description with something that tells the
  agent when it should call the skill. Triggers, parameters, and the worker
  live alongside this file.

triggers:
  - "example"

supports_raw: false

parameters:
  type: object
  properties:
    message:
      type: string
      description: Text to echo back
  required:
    - message
"""

_WORKER_PY = '''\
"""Example skill worker. Replace with your real implementation.

The worker contract: receive a typed `SkillContext` (tenant id, secrets,
logger, blob store) and return a `SkillResult`. Use `SkillResult.ok(...)`
for happy paths and `SkillResult.fail("...")` for errors.
"""

from __future__ import annotations

from typing import Any

from t3nets_sdk.contracts import SkillContext, SkillResult


async def execute(ctx: SkillContext, params: dict[str, Any]) -> SkillResult:
    message = params.get("message", "")
    return SkillResult.ok({"echo": message})
'''

_TEST_PY = '''\
"""Example test for the example skill.

Loads the worker by file path so the test runs without any pytest config —
the platform itself loads workers the same way at install time.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from t3nets_sdk.contracts import SkillContext, SkillResult


def _load_worker():
    path = Path(__file__).resolve().parent.parent / "skills" / "example" / "worker.py"
    spec = importlib.util.spec_from_file_location("example_worker", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_example_echoes_message() -> None:
    worker = _load_worker()
    ctx = SkillContext(tenant_id="test-tenant")
    result: SkillResult = asyncio.run(worker.execute(ctx, {"message": "hello"}))
    assert result.success
    assert result.data == {"echo": "hello"}
'''

_README = """\
# {display}

A t3nets practice.

## Development

```bash
pip install t3nets-sdk
t3nets practice validate
t3nets practice package
```

The resulting `dist/practice.zip` can be uploaded to any t3nets deployment.
"""


def _display_name(name: str) -> str:
    return name.replace("-", " ").replace("_", " ").title()


def run(args: argparse.Namespace) -> int:
    name: str = args.name
    if not _NAME_RE.match(name):
        print(
            f"error: invalid practice name {name!r}. "
            "Use letters, digits, dashes, underscores; must start alphanumerically.",
            file=sys.stderr,
        )
        return 2

    parent = Path(args.dir).resolve()
    dest = parent / name
    if dest.exists():
        print(f"error: {dest} already exists", file=sys.stderr)
        return 2

    display = _display_name(name)
    files: dict[str, str] = {
        "practice.yaml": _PRACTICE_YAML.format(name=name, display=display),
        "skills/example/skill.yaml": _SKILL_YAML,
        "skills/example/worker.py": _WORKER_PY,
        "skills/example/__init__.py": "",
        "tests/test_example.py": _TEST_PY,
        "README.md": _README.format(display=display),
    }
    for rel, content in files.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    print(f"Created practice at {dest}")
    print("Next: cd into it and run `t3nets practice validate`.")
    return 0
