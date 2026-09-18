# Public References for Tool Authorization

Research date: September 16, 2026. This is a source review, not an implementation or a reproduction of the projects' reported results.

## Primary Reference: Progent

- Repository: https://github.com/sunblaze-ucb/progent
- Paper: https://arxiv.org/abs/2504.11703
- Tool-call middleware: https://github.com/sunblaze-ucb/progent/blob/main/secagent/progent_proxy.py
- Policy analysis: https://github.com/sunblaze-ucb/progent/blob/main/secagent/policy_analysis.py
- AgentDojo integration notes: https://github.com/sunblaze-ucb/progent/blob/main/agentdojo/README.md

Progent is closely aligned with the proposed next step: deterministic privilege checks over tool calls and arguments, with an existing AgentDojo evaluation. Its MCP middleware checks the tool name and arguments before dispatch. The paper also describes policy generation and updates using LLMs; those are separate from deterministic policy enforcement and should not be treated as trusted user authorization automatically.

The AgentDojo README documents a scoring issue in one Slack injection task: a blocked call remaining in the trace can be counted as an attack. When expanding our suite, preserve official scores and separately inspect actual execution and state changes; disclose any scoring correction instead of silently changing the benchmark.

## CaMeL

- Repository and AgentDojo run instructions: https://github.com/google-research/camel-prompt-injection
- Policy engine: https://github.com/google-research/camel-prompt-injection/blob/main/src/camel/security_policy.py

CaMeL checks tool arguments and dependencies using data capabilities, with denial when no policy matches. This is a useful reference for data provenance and information-flow restrictions. It is a substantially different architecture from a small approval gate. The repository explicitly describes itself as an unsupported research artifact, not a production security product.

## Invariant Guardrails

- Local policy examples: https://github.com/invariantlabs-ai/invariant/blob/main/README.md

Supports matching tool arguments and relationships between calls. It provides a reusable rule engine rather than requiring an application to invent a policy language. Assess dependencies, integration effort, and licensing before adopting code.

## LangChain Human-in-the-Loop Middleware

- Implementation: https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py

Represents an approval request with the action name and arguments, with approve/edit/reject decisions and optional argument-dependent interruption. This provides an engineering reference for reviewing a specific proposed call. Its documented default automatically approves tools without an interrupt configuration, so it should not be adopted as a deny-by-default authorization policy without additional controls.

## Proposed Direction

Prioritize Progent's parameter-aware enforcement and AgentDojo integration, with LangChain as a reference for explicit approval handling. First assess whether Progent's policy evaluator can be reused cleanly; do not claim reuse merely because the design is similar. Keep the current MiniAgent execution gate and benchmark adapter until a replacement has been verified.

Authorization must come from trusted user input or a declared approval fixture, not official ground-truth answers or attacker identifiers. An approval fixture changes the evaluation assumptions and must be reported. Binding approval to a complete call, limiting reuse, and rejecting changes to approved parameters are proposed requirements for our implementation, not claims that all referenced projects implement them.
