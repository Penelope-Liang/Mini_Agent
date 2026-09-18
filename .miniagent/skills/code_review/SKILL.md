---
name: code_review
description: Review code for bugs, security issues, regressions, maintainability risks, and missing tests.
user-invocable: false
when-to-use: Use when the user asks to review code, code review a file or diff, inspect code quality, find bugs, identify security issues, evaluate maintainability, or check for missing tests.
---

# Code Review Skill

Use this skill when reviewing source code, diffs, or implementation changes.

## Review Focus

Prioritize concrete findings over summaries. Focus on issues that can cause incorrect behavior, security exposure, regressions, data loss, resource leaks, or meaningful maintainability problems.

Check for:

- Correctness bugs and edge cases
- Security vulnerabilities, including injection, unsafe file paths, leaked secrets, and untrusted input handling
- Async, concurrency, lifecycle, and cancellation issues
- Error handling gaps and resource leaks
- API contract mismatches, invalid payload formats, and incompatible parameter names
- Missing or weak tests for changed behavior
- Maintainability risks that are likely to create future defects

## Review Method

Read the relevant files before making claims. If the user provides a file path, review that file directly. If the user provides a diff or describes a change, focus on the changed behavior and affected call sites.

Treat initial suspicions as candidates. For each candidate, identify the exact source location, triggering input or state, concrete incorrect behavior, and supporting code. Diff line numbers are not source line numbers.

Before reporting, try to disprove each candidate: inspect relevant callers, existing guards, documented contracts and tests. Follow references that could change the conclusion. If a file is truncated, read the relevant source range using `read_file` with `offset` and `limit`; a preview does not establish coverage of the whole file.

Keep findings supported by a reachable failure, not merely a hypothetical concern. Drop claims contradicted by the code. Put unresolved candidates and missing context under open questions; do not promote them to confirmed bugs or claim the unreviewed code is safe. State whether tests were executed or only inspected.

Do not list generic best practices unless they apply to the code being reviewed. If there are no clear issues, say so and mention any remaining test gaps or residual risk.

## Output Format

Put findings first, ordered by severity. Include file and line references when possible.

Use this structure:

1. Findings
   - Severity
   - Location
   - Problem
   - Why it matters
   - Suggested fix
2. Open questions or assumptions
3. Test gaps or residual risk

If no issues are found, write: "No blocking issues found." Then list any test gaps or residual risk.
