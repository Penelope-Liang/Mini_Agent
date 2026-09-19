# Permission Enforcement Verification (Local, No Model Calls)

## Reproduce

After installing `requirements.txt`, run from the MiniAgent project root:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m examples.permissions_demo
```

Local results on September 16, 2026: 18 tests passed, including 14 permission tests and 4 existing general tests. The demo rejected 3 unauthorized calls, preserved the configuration file's SHA-256 hash, and produced 9 audit events. It made no model calls and incurred no model fees.

On September 18, 2026 the suite is 28 tests, all passing, after per-change approval and the argument-rendering work below. Reverting `describe_call` fails two of the three new tests; the third, that a refused approval never reaches the tool, passes either way because that behaviour predates this change and the test guards it against regression. These are deterministic tests without model calls, so they are reported as counts passed and never as `pass^k`, which measures reliability under repeated sampling of a stochastic agent.

Artifacts: `artifacts/permissions/result.json`, `audit.jsonl`, and simulated logs and configuration. Running the demo again recreates these artifacts.

## Execution Path

`skill` tool → load project `ops-troubleshooting/SKILL.md` → create a fork Agent → intersect the parent's tools with the Skill allowlist → route each call through `_execute_tool_call` → evaluate permissions → audit → execute or deny.

The demo replaces the model client and the `run_once` model loop with a fixed script that reads evidence, returns diagnosis text, and attempts unauthorized calls. Skill loading, fork creation, file tools, permission enforcement, and auditing use the real implementation. This verifies execution boundaries, not model diagnostic accuracy or prompt-injection attack success rates.

## Fixes and Checks

| Boundary | Verified behavior |
|---|---|
| Shared execution entry point | Direct calls check the permission mode and tool allowlist; both model backends use this entry point |
| plan | Rejects ordinary writes, shell commands, Skill mutations, and unclassified MCP tools; permits reads and writes to the designated plan file; allow rules cannot override plan restrictions |
| dontAsk | Rejects writes and shell calls that require approval without prompting |
| default | Requires fresh approval for each file write; rejected calls do not write |
| acceptEdits | Allows edits, but existing files must be read first; external mtime changes require another read |
| fork | Skill forks and general child Agents inherit the permission mode and cannot expand the parent's tools; empty allowlists remain empty |
| bypassPermissions | Skips mode and rule checks, but still respects the Agent's tool set |
| Leaving plan mode | Requires an approval callback; explicit approval changes the mode |
| Read tracking | Binary-file and beyond-EOF reads do not authorize overwriting |
| Paths | Absolute paths are no longer silently redirected to other workspace files; plan-file checks use resolved paths |
| Approval content | Confirmations render the arguments of the proposed call, so an approver of a non-file tool sees the recipient and amount rather than a bare tool name |
| Credential handling | Argument keys naming a password, token, secret, credential, or API key render as `[REDACTED]` in the prompt and in the audit record |
| Audit | Records session/call IDs, mode, tool, decision, approval result, and completion status; failure to persist authorization prevents execution |

Behavior change: all shell calls and unclassified external tools require confirmation unless explicitly allowed. Safety no longer depends on recognizing dangerous command strings. `acceptEdits` automatically permits editing tools only. Explicit allow rules can preauthorize calls outside plan mode; explicit deny rules take priority in modes other than bypassPermissions.

## Scope and Limitations

- Enforcement operates inside the application. A fork has a separate context, not an operating-system sandbox. Production RBAC is not implemented.
- `allowed-tools` is enforced for forks. Inline Skills inject prompts and do not provide permission isolation.
- Plan mode restricts model-initiated tool operations; the runtime still writes audit events, Skill usage statistics, and other metadata.
- MCP tools lack a trusted read-only classification, so plan mode rejects them by default. No live logging platform was connected for this verification.
- mtime checks detect ordinary external changes. They do not provide atomic writes or protect against races or deliberate timestamp preservation.
- Audit records are local JSONL files, not tamper-proof logs. They do not authenticate the approver. Approval reasons contain commands, paths, and other tool arguments; only credential-named keys are withheld, so a secret passed under an unrecognised key name would still be recorded.
- `agents.tools.execute_tool` is an internal handler. Integrations must call the Agent execution entry point to receive mode and allowlist checks.
- This verification did not run AgentDojo, ITBench, real models, or production incidents. Results are not public benchmark scores or evidence of production performance.

## Follow-up: Real-model Evaluation

The initial verification above was offline. A subsequent [AgentDojo pilot](agentdojo-evaluation.md) ran eight real GPT-4o scenarios with official tasks, attacks, and programmatic scoring. Its results are reported separately from component tests and the deterministic demo.

That evaluation later measured human approval itself and found that approval at tool granularity did not reduce attack success at all, which is what motivated argument rendering and the isolated approver in `agents/approval.py`. The approver is a model call and therefore probabilistic; nothing in this document's deterministic guarantees covers it.
