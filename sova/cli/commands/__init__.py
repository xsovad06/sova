"""Typer command implementations, registered by ``sova.cli.app``.

This package holds the ``sova`` CLI's own subcommands (``sova run``, ``sova server``, ...). Not to be confused
with ``sova.commands``, which manages the canonical slash-command templates installed into target projects
(catalog, distribution, manifest, staleness).

Allowed imports: sova.dashboard and every layer below it, plus the sibling layer sova.scheduler.
Outside the layers contract (import-linter does not check these): sova.agents, sova.awareness, sova.commands,
  sova.knowledge, sova.mcp.
"""
