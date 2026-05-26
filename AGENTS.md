Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

## 5. Testing Policy For This Repo

**Default tests must not spend Runpod money or credits. Local e2e should still be real.**

- Do not make default tests create paid Runpod pods, volumes, or other billable remote resources.
- Keep live Runpod tests explicit and opt-in behind environment variables.
- Prefer fake Runpod clients for unit/integration coverage of remote API behavior.
- Local e2e tests should spawn real local containers and run the actual ComfyUI node paths whenever a supported runtime is available.
- Do not replace local e2e with pure mocks or static plan checks unless the test is specifically named/documented as an offline fallback.
- If the required local container runtime is missing, the local e2e test should fail clearly instead of silently skipping.
- If CI cannot run nested containers, gate the local e2e job at the CI workflow level rather than weakening the test itself.

## 6. CRAG Node Effect Taxonomy

**For agentic workflow edits, classify nodes by their runtime effect.**

- Queue commands: `SSH Command`, `Package`, and `Language Runtime` produce command chains for `Deploy.commands`.
- Queue implicit commands: `Agent`, `Local SQL Database`, `Skill`, and `Skill Framework` add setup commands through runtime contracts.
- Add containers: `Agent`, `Browser` with `own_pod`, `LLM Server`, `Remote SQL Database` with `own_pod`, and `Vector Database` add pod/service resources.
- Add storage or env only: `Network Storage`, `S3 Storage`, `LLM API`, `MCP Server`, `Subagent`, `Remote SQL Database` with `env_only`, and `Local SQL Database` add volumes, env, secrets, or config.
- Assemble policy: `Deploy`, `Keep Alive`, and `SSH Access` build graph/lifecycle/access policy. `Deploy` is graph-only; Runpod placement belongs on `Run on Runpod`.
- Terminal nodes: `Run on Runpod`, `Run Local Containers`, `Build Container`, `Compose YAML`, `Startup Script`, and `Logs` end workflow branches by planning, applying, exporting, building, or reading results.

## 7. Example Workflow Policy

**Keep examples focused on graph shape, not duplicated run actions.**

- Do not create separate `*_up.json` and `*_down.json` examples when the only difference is a terminal node's `mode` or `action`.
- Keep one API workflow per scenario, defaulted to the useful apply/plan action.
- For teardown, update the terminal node action in memory or in the UI, for example change `Run Local Containers.action` from `apply` to `terminate`.
- When renaming example workflows, update README, `docs/user_guide.md`, and any e2e tests that submit those examples.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
