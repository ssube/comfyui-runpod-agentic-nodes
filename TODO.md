# TODO

Items that need real Runpod template details, credentials, or runtime image decisions:

- Verify exact Runpod GraphQL input object names against the live API account and adjust mutations if the account requires a newer schema.
- Replace the placeholder `/usr/local/bin/runpod-agent-launch` command with the final runtime supervisor command baked into agent templates.
- Define real template IDs for `rp-agent-*`, `rp-browser-*`, `rp-llm-*`, `rp-db-*`, and `rp-vector-*`.
- Add live integration tests behind `RUNPOD_API_KEY` and a test template ID.
- Add richer readiness probes for HTTP services after template endpoint shapes are finalized.
- Add direct remote pod log streaming if Runpod exposes a stable logs API for pods; current log collection reads local command logs captured by `Runpod Run`.
