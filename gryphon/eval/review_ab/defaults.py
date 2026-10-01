"""Defaults of ``gryphon review-eval``, importable without the runner.

``cli.add_parser`` runs on every ``gryphon`` invocation to build the argument
parser; importing the runner there pulled subprocess, YAML and the sandbox
into every command, including the per-tool-call hooks.
"""

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_EFFORT = "high"
DEFAULT_BUDGET_USD = 3.0
DEFAULT_TIMEOUT_S = 2400
