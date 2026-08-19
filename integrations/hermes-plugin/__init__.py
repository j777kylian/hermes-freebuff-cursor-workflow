"""Hermes plugin: deterministic slash commands for the workflow bridge.

Registering commands only; no LLM is invoked by any handler. Each handler
forwards to the ``workflow`` CLI with argument arrays (shell=False).
"""

from . import commands


def register(ctx):
    commands.register(ctx)
