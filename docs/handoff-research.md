# Handoff preview: research and scope

Research date: 2026-09-04. This is a bounded public-source comparison, not a patent
search or proof of worldwide novelty. Search indexes miss unpublished work, private
products, and features documented under different names. No "world first" claim is made.

## What already exists

| Primary source | Existing approach | Implication for Thwip |
| --- | --- | --- |
| [Aider chat modes](https://aider.chat/docs/usage/modes.html) | Architect and editor model pairing | Multi-model collaboration alone is not novel. |
| [Aider model warnings](https://aider.chat/docs/llms/warnings.html) and [token limits](https://aider.chat/docs/troubleshooting/token-limits.html) | Metadata warnings, token accounting, and overflow guidance | Model-fit diagnostics alone are not novel. |
| [OpenCode compaction](https://opencode.ai/v2/docs/compaction) | Structured checkpoints plus recent context; durable messages retained separately | Summaries and checkpoints alone are not novel. |
| [OpenRouter fallbacks](https://openrouter.ai/docs/guides/routing/model-fallbacks) | Ordered fallback models on request failures | Routing and failover alone are not novel. |
| [OpenRouter message transforms](https://openrouter.ai/docs/guides/features/message-transforms) | Context compression by removing or truncating messages | Automatic fitting can trade away recall. |
| [Agent Handoff](https://github.com/AniruddhaHumane/handoff) | Portable file-backed resume briefs across agents | Cross-agent memory alone is not novel. |
| [rosehgal/handoff](https://github.com/rosehgal/handoff) | Append-only action log and rendered handoff document | Tool-action tracking and durable handoffs already exist. |

Searches included combinations of "coding agent", "model switch", "handoff",
"context loss report", "preflight", "capability loss", "fingerprint", and "receipt",
as well as product-specific documentation queries. Sources were inspected through
web search retrieval. No restricted pages or private data were scraped.

## Chosen differentiation

Thwip already owns the provider switch boundary. Put a local continuity report at
that boundary: exact text-message coverage, explicit state omissions, capability
differences, advisory context pressure, and a deterministic text fingerprint together.
The examined sources establish prior art for the components; they did not establish
the same integrated report. That is an opportunity hypothesis, not proof of uniqueness.

## Implementation boundaries

- `/handoff [provider] [model]` is a non-mutating dry run. `/switch` displays it too.
- No new dependencies, model calls, summarization costs, or external data storage.
- Only a result count is added to saved sessions, not raw tool inputs or outputs.
- Legacy sessions explicitly show incomplete tool-result tracking.
- SHA-256 covers canonical system prompt and portable text, not provider wire formats,
  delivery, hidden reasoning, tool schemas, project file contents, or model comprehension.
- Context estimates are heuristic and warnings advisory. Provider-native tool
  continuation correctness remains a separate issue, not solved by this report.
- Production deployment and other defects from the previous audit remain separate work.

## Validation

Offline tests cover stable/change-sensitive fingerprints, exclusions, capability
gains/losses, context thresholds and unknown limits, unknown targets, private session
persistence, legacy migration, clear commands, dry-run non-mutation, safe terminal
rendering, switch integration, and command completion. Live generation is not needed
to validate this diagnostic, and no claim about live provider success follows from it.
