"""A/B review benchmark: the same agent reviews the same PR with and without the graph.

Each case is a closed pull request (base and head commits). For every case the
harness prepares one sanitized clone per arm — history only up to the PR head,
agent configuration stripped — and runs ``claude -p`` in restricted mode:

- ``baseline``: Bash, Read, Grep and Glob only.
- ``graph``: the same tools plus the gryphon MCP server over a graph built at
  the PR head.

Both arms get the same review prompt except for the section describing the
tools, and both return findings through the same JSON schema, so a later
judging pass can score them side by side.
"""
