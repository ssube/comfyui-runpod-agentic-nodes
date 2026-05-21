# TODO

Most repo-local TODO items have been implemented. The remaining work needs confirmed Runpod account behavior, final runtime images, or provider APIs.

## Completed In Repo

- Added `scripts/check-runpod-schema` to validate the GraphQL input object names and required fields used by CRAG pod and template operations.
- Added opt-in live pytest coverage behind `RUNPOD_LIVE_TESTS`, with real pod creation additionally gated by `RUNPOD_LIVE_CREATE_POD`, `RUNPOD_TEST_TEMPLATE_ID`, and `RUNPOD_TEST_GPU_TYPE_ID`.
- Added HTTP readiness probes for known public service endpoints such as Ollama, vLLM, Qdrant, and Chroma.
- Added real template IDs to `defaults/runpod_template_ids.json` for the current bootstrap templates.
- Added an SSH-injected `.runpod_agentic` runtime layer with `launcher.sh`, `.d`-style hooks, and common harness stubs so CRAG does not need to bake the launcher shim into every image.
- Made the exact agent launch command configurable with `CRAG_AGENT_LAUNCH_COMMAND`.

## Remaining External Work

- Publish production images with the desired agent CLIs installed, or standardize the deployment environment on `CRAG_AGENT_LAUNCH_COMMAND` for images that need custom startup behavior.
- Re-run `scripts/check-runpod-schema --json` after Runpod schema changes and adjust mutations if the account exposes a newer API shape.
- Replace bootstrap template images and commands with production CRAG runtime images once those images are published.
- Finalize richer service-specific readiness probes after production template endpoint shapes are locked.
- Add direct remote pod log streaming if Runpod exposes a stable pod logs API. Current log collection reads local command logs captured by `Run on Runpod`.
- Figure out the supported Runpod API path for CPU-only pods and add first-class CRAG support. `CPU` is not returned by `gpuTypes`, and `podFindAndDeployOnDemand` currently rejects omitted `gpuTypeId`.
