# Greeny foundation test report

Date: 2026-10-08. Branch: `feature/greeny-sales-foundation`.

## Scope

Completed the repository audit, prioritized roadmap and smallest safe first change:
standalone `app/sales_state.py`. Existing API, graph, tools, sessions, deployment,
catalogue and RAG behavior are unchanged. The new model is not imported into the
runtime yet. No packages were installed and no external resources were accessed by
tests. No database writes, paid inference, push, merge or deployment were performed.

## Executed checks

| Check | Result |
| --- | --- |
| Baseline `python -m pytest tests -q -p no:cacheprovider` | 4 passed in 0.19s |
| After first coding phase, same command | 39 passed in 0.22s |
| `git diff --check` for tracked changes | Passed; new files additionally reviewed directly |
| Installed LangGraph API inspection | `MemorySaver.delete_thread` and `StateGraph.compile(checkpointer=...)` available |

The 35 new parametrized cases cover anonymous defaults, all stage destinations,
multi-requirement updates, date and money serialization, invalid/non-finite values,
strict quantity and escalation flags, immutable identity, snapshot isolation,
consent refusal/revocation preservation, unknown fields and absent versus zero budget.

These are **state-contract tests, not 30 end-to-end sales scenarios**. Intent
accuracy, recommendation relevance, unsupported-claim rate, lead qualification and
model language quality have not been measured. No performance or production
readiness claim is made. Core runtime risks are recorded in the audit and remain
open, including stale stock, unsupported certification claims and public errors.

## Run locally in Windows / VS Code

Open the repository folder in VS Code and select `.venv\Scripts\python.exe`.
In its PowerShell terminal, from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
```

Equivalent Ubuntu test command in an existing environment:

```bash
.venv/bin/python -m pytest tests -q -p no:cacheprovider
```

No API keys, MongoDB connection or model download are needed for these tests.
Do not run `scripts/live_smoke_test.py` as a substitute for this offline suite:
it uses configured inference and RAG. Deployment commands in the existing Ubuntu
guide were not executed; its multiworker/session limitation must be resolved first.

## Next reviewable change

Phase 1: add offline API/WS regression tests and address generic public errors,
request bounds and session ownership. Then introduce typed product/business-service
adapters with mock fixtures, before attaching MongoDB to the agent. See
`GREENY_ARCHITECTURE_AUDIT.md` for gates, outstanding inputs and later documentation.
