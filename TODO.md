# TODO

Most repo-local TODO items have been implemented. The remaining work needs confirmed Runpod account behavior, final runtime images, or provider APIs. Keep this file focused on open work; use README and `docs/user_guide.md` for feature documentation.

## Completed In Repo

- Added `scripts/check-runpod-schema` to validate the GraphQL input object names and required fields used by CRAG pod and template operations.
- Added opt-in live pytest coverage behind `RUNPOD_LIVE_TESTS`, with real pod creation additionally gated by `RUNPOD_LIVE_CREATE_POD`, `RUNPOD_TEST_TEMPLATE_ID`, and `RUNPOD_TEST_GPU_TYPE_ID`.
- Added HTTP readiness probes for known public service endpoints such as Ollama, vLLM, Qdrant, and Chroma.
- Added real template IDs to `defaults/runpod_template_ids.json` for the current bootstrap templates.
- Added an SSH-injected `.runpod_agentic` runtime layer with `launcher.sh`, `.d`-style hooks, and common harness stubs so CRAG does not need to bake the launcher shim into every image.
- Made the exact agent launch command configurable with `CRAG_AGENT_LAUNCH_COMMAND`.
- Added a harness compatibility matrix and common wrappers for Codex, Claude, OpenCode, Hermes, and Pi. Current system-prompt CLI support is Claude and Pi only.
- Added live local harness install e2e coverage through `scripts/e2e-local-harness-installs-live`; each supported harness installs in a fresh container and is probed through an `SSH Command` node.
- Added `Run Local Containers` support for Docker, Podman, and containerd Compose projections, including stale-container reconciliation and local named-volume behavior.
- Added `Build Container` as a terminal backend for local container snapshots, with unit and live local e2e coverage.
- Added local and Runpod keep-alive timeout coverage, including startup-command interaction and harness CLI argument forwarding where supported.
- Added ttyd web terminal support for local runtimes and Runpod proxy endpoints. Terminal examples now use one workflow per scenario and switch the run action for teardown.
- Added Runpod datacenter and GPU type option lookup for `Run on Runpod` and `Network Storage` dropdowns.
- Consolidated API example workflow pairs: do not keep separate up/down examples when the only difference is the terminal action.
- Declared `aiohttp` as a runtime dependency because the route/proxy code imports it directly.

## Remaining External Work

- Publish production images with the desired agent CLIs preinstalled for faster launches, or standardize the deployment environment on `CRAG_AGENT_LAUNCH_COMMAND` for images that need custom startup behavior.
- Re-run `scripts/check-runpod-schema --json` after Runpod schema changes and adjust mutations if the account exposes a newer API shape.
- Replace bootstrap template images and commands with production CRAG runtime images once those images are published.
- Finalize richer service-specific readiness probes after production template endpoint shapes are locked.
- Add direct remote pod log streaming if Runpod exposes a stable pod logs API. Current log collection reads local command logs captured by `Run on Runpod`.
- Figure out the supported Runpod API path for CPU-only pods and add first-class CRAG support. `CPU` is not returned by `gpuTypes`, and `podFindAndDeployOnDemand` currently rejects omitted `gpuTypeId`.
