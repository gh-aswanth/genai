---
name: workflow-planning
description: Plan every request as a todo list with write_todos - the todo list is the single source of truth for the plan (no plan files) - with one concrete, checkable item per workflow step; execute it in order, revise it with write_todos when results or the user's new input change the plan, and complete every item before answering. Required for every request that delegates work.
---

# The todo list is the plan

There is one plan and it lives in your todo list (`write_todos`). No plan
documents, no progress files: the user sees the todo list live, and it is what
the checks read.

- `task` is refused until this request's todo list exists.
- You cannot give your final answer while any item is `pending` or
  `in_progress` - you are sent back with the open items until each is completed
  or removed for a stated reason.
- Every new user message is a new request: revise the list with `write_todos`
  before delegating again.

## 1. Write the plan (before any `task`)

Pick the workflow with `agent-orchestration`, read its skill(s), then call
`write_todos` ON ITS OWN (not in the same message as other tools). One item per
step, in execution order, numbered, the first one `in_progress`. Each item says
who does what, from what, to what, and how you will know it is done:

```
1. job-search: 25 senior Python jobs, Kochi (remote OK) -> /output/jobs/jobs.json | done: >= 1 job, fields normalised
2. job-matcher: resume + jobs.json, pick best 2 -> /output/match/match_report.json | done: 2 selected_jobs with changes
3. [job 1: <slug>] job-optimizer (parallel with 4) -> /output/resume/1-<slug>/ | done: 3 deliverables + ats_history.json
4. [job 2: <slug>] job-optimizer (parallel with 3) -> /output/resume/2-<slug>/ | done: 3 deliverables + ats_history.json
5. verify all jobs' deliverables | done: every job has redline, review, final .docx
6. memory: save durable notes | done: memory files updated
7. output-cleanup: keep deliverables only | done: KEEP list checked
```

Put the run settings where they apply (N jobs, target score, rounds, sites,
constraints from the user or memory), so each item can be executed from its
text alone. Every "done" is something you verify from files, not from a
subagent's message.

## 2. Execute in order

For each item: set it `in_progress` -> delegate / do it -> verify its "done"
from the files -> set it `completed`. One `write_todos` call per change, never in
parallel with another `write_todos`. Items the workflow runs in parallel (one
job-optimizer per job) are set `in_progress` together and delegated in ONE
message; each is completed when its own job's deliverables check out.

## 3. Change the plan - always through `write_todos`

Rewrite the list whenever reality or the user changes the plan. Keep completed
items as they are, edit / add / remove the not-yet-done ones, keep the numbering
in execution order, and say in your next message what changed and why.

| Trigger | Change |
|---|---|
| a job's optimizer failed or left deliverables missing | re-launch that one job's optimizer; the others stay completed |
| a site blocked / needed login | edit the search item: next site, note the skipped one |
| a step failed twice | mark what was done, remove the rest of that branch, carry on |
| **the user adds or changes something** ("also Bangalore", "only 1 job", "skip cleanup", "stop after round 1", "use this job URL instead") | apply their words to the pending items: add, remove, reorder, change parameters - then continue with the next item |
| the user asks a question mid-run | answer it; if it changes nothing, the list stays as it is |

Never remove an item just to be able to finish: remove it only when the user
asked, or the step no longer applies - and say which case it is.

## 4. Finish

Everything `completed`, then answer: what the user asked for first (per job:
title, company, link, scores per round, main changes, open questions), then the
deliverable paths.
