# Vadim Workspace Boundary

This subtree is the isolated workspace for Vadim's agent-memory experiments.

- Keep implementation, tests, experiment artefacts and normative design changes
  under `vadim/`.
- Do not modify sibling `simple_agent/`, repository-root documentation or other
  contributors' files unless the user explicitly expands the scope.
- Treat `docs/agent-memory-design/` as the normative source for staged work.
- Preserve the copied baseline while Stage 0 is measured; introduce richer
  memory behavior only through the documented stage gates.
- Run Python commands from this directory with `uv run ...`.
- Never commit `.env`, credentials, local virtual environments, caches or runtime
  `artifacts/`.
