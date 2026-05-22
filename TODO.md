# TODO

Keep this file focused on open work. Completed features belong in README, `docs/user_guide.md`, release notes, or commit history.

## External Work

- Publish production images with the desired agent CLIs preinstalled for faster launches, or standardize the deployment environment on `CRAG_AGENT_LAUNCH_COMMAND` for images that need custom startup behavior.
- Re-run `scripts/check-runpod-schema --json` after Runpod schema changes and adjust mutations if the account exposes a newer API shape.
- Replace bootstrap template images and commands with production CRAG runtime images once those images are published.
- Finalize richer service-specific readiness probes after production template endpoint shapes are locked.
- Add direct remote pod log streaming if Runpod exposes a stable pod logs API. Current log collection reads local command logs captured by `Run on Runpod`.
- Figure out the supported Runpod API path for CPU-only pods and add first-class CRAG support. `CPU` is not returned by `gpuTypes`, and `podFindAndDeployOnDemand` currently rejects omitted `gpuTypeId`.
