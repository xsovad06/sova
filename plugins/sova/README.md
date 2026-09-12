# SOVA Plugin

Autonomous AI-assisted development workflow commands from [SOVA](https://github.com/xsovad06/sova) (Software Orchestration Via Agents).

## Commands

### `/sova:develop`

Implement a feature or fix using test-driven development, then verify with the project's test suite.

### `/sova:spec`

Produce a structured specification document for a task before development starts, shifting design decisions to the cheap planning phase instead of the expensive coding phase.

### `/sova:review`

Review changed code as a senior engineer before pushing. Scores findings by priority and addresses all of them.

### `/sova:pr`

Create a pull request with a standard template, analyzing all commits and changes on the current branch.

### `/sova:debug`

Systematic debugging workflow: reproduce, locate, diagnose, fix, verify, prevent.

### `/sova:test`

Run the project's linter and test suite iteratively until both pass.

## Installation

```bash
/plugin install sova@ai-helpers
```

## About SOVA

SOVA is a standalone application that takes issues from a tracker, develops solutions using TDD, self-reviews, creates pull requests, monitors CI, and addresses review feedback autonomously. These commands are a subset of SOVA's full pipeline, adapted to run standalone in any project without installing SOVA itself.

See the [SOVA repository](https://github.com/xsovad06/sova) for the full autonomous pipeline, dashboard, and multi-project orchestration.
