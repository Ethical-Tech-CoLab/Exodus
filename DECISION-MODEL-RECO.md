# Decision-model recommendation: source triage before country narrative

Date: 2026-10-11. Public-source proposal; no API calls.
Reviewed [README](https://github.com/Ethical-Tech-CoLab/Exodus/blob/0d8ee48b0a8108c4fd4354b4e8d1a98dcc766e37/README.md).
This is a documentation-led assessment, not a full backend review.

## Concrete seam to inspect

The documented FastAPI service combines crisis feeds, scenario calculations
and risk assessment, including a Claude-powered country-context endpoint.
Before that narrative stage, test source relevance and claim-support triage
over already retrieved public reports. Retain generative synthesis where prose
is required; a decision model cannot replace the full narrative endpoint.

Input: crisis/query, report excerpt, source and reporting period. Output
relevant / unrelated / ambiguous, and supports / contradicts / not established.
Keep feed freshness, retrieval failures and source coverage visible.

## Keep out of scope

Do not change resource, casualty, risk or cost calculations; infer legal
compliance; provide operational evacuation advice; or merge distinct index
scales into a model probability. Shared backend/API keys do not make the three
tools' assumptions interchangeable.

## Evaluation

Compare unchanged retrieval/Claude with lexical and local reranking, Jev's
[reranking recipe](https://docs.typesafe.ai/cookbooks/rerank_typesafe.md),
Microsoft-Decision-1 and Sage on public fixtures. First inspect the actual
endpoint implementation and its fallback contract before coding.

Split by crisis/report family and time. Measure candidate recall, retained
relevant sources, unsupported-claim acceptance, calibration, latency and full
cost per accepted sourced narrative, including remaining Claude calls and
human review. Cheap triage is useful only if it removes work without losing
critical evidence.

## Gate

Server-side shadow adapter only; no changed application advice or calculations.
No secrets in static pages, no silent conversion of failures into low risk.
Exact proprietary checkpoints are not local models; local alternatives require
independent evaluation. No measured improvement is claimed.
