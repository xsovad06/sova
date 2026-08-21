# YAML Workflow Definitions

This directory contains YAML-defined custom workflows for SOVA. YAML workflows provide a declarative way to define custom agent workflows without writing Python code.

## Usage

Run a YAML workflow using:

```bash
sova run --workflow <workflow-name> <issue-number>
```

Or using the `--role` option (equivalent):

```bash
sova run --role <workflow-name> <issue-number>
```

## YAML Schema

```yaml
name: workflow-name              # Required: unique identifier
description: Workflow description # Optional: human-readable description

steps:
  - name: step1                   # Required: unique step identifier
    command: command-name         # Required: SOVA command to execute
    prompt_template: "..."        # Optional: prompt template for the command
    timeout: 600                  # Optional: timeout in seconds
    model: sonnet                 # Optional: LLM model to use

  - name: step2
    command: another-command
    prompt_template: "..."
    depends_on: [step1]           # Optional: step dependencies (list)
    condition: "{{ step1.done == 'true' }}" # Optional: Jinja2 condition
```

## Conditions

Conditions use Jinja2 template syntax and support:

- Simple equality: `{{ step1.status == 'completed' }}`
- Boolean logic: `{{ step1.done == 'true' and step2.done == 'true' }}`
- Filters: `{{ step1.count|int > 5 }}`

Legacy simple conditions are also supported:
- `step1.done == true`
- `step1.status != failed`

## Discovery

YAML workflows are discovered from:
1. `commands/workflows/` (project-level, checked into version control)
2. `.sova/workflows/` (project-specific, typically gitignored)

Both `.yaml` and `.yml` extensions are supported.

## Validation

Workflows are validated for:
- Required fields (`name`, `steps`)
- Circular dependencies (cycle detection)
- Undefined step references in `depends_on`
- Name conflicts with built-in roles (developer, researcher, reviewer, triage, planner)
- Empty step lists
- YAML syntax errors

Invalid workflows are logged and skipped during discovery.

## Example

See `example-docs-workflow.yaml` in this directory for a complete example.
