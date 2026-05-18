# TODO

Most repo-local TODO items have been implemented. The remaining work needs confirmed Runpod account behavior, final runtime images, or provider APIs.

## Completed In Repo

- Added `scripts/check-runpod-schema` to validate the GraphQL input object names and required fields used by CRAG pod and template operations.
- Added opt-in live pytest coverage behind `RUNPOD_LIVE_TESTS`, with real pod creation additionally gated by `RUNPOD_LIVE_CREATE_POD`, `RUNPOD_TEST_TEMPLATE_ID`, and `RUNPOD_TEST_GPU_TYPE_ID`.
- Added HTTP readiness probes for known public service endpoints such as Ollama, vLLM, Qdrant, and Chroma.
- Added real template IDs to `defaults/runpod_template_ids.json` for the current bootstrap templates.
- Made the agent launch command configurable with `CRAG_AGENT_LAUNCH_COMMAND`.

## Remaining External Work

- Bake the final runtime supervisor into agent templates as `/usr/local/bin/runpod-agent-launch`, or standardize the deployment environment on `CRAG_AGENT_LAUNCH_COMMAND`.
- Re-run `scripts/check-runpod-schema --json` after Runpod schema changes and adjust mutations if the account exposes a newer API shape.
- Replace bootstrap template images and commands with production CRAG runtime images once those images are published.
- Finalize richer service-specific readiness probes after production template endpoint shapes are locked.
- Add direct remote pod log streaming if Runpod exposes a stable pod logs API. Current log collection reads local command logs captured by `Run on Runpod`.
