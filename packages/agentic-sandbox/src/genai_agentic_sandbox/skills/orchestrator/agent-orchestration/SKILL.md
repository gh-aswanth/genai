---
name: agent-orchestration
description: How the JobHunter orchestrator works - pick the workflow skill that fits the request (full job hunt, optimise resume for a given job, ATS score only, find jobs only, cleanup), plan it as todos, brief and run subagents (in parallel where independent), verify every output, recover from failures, and report. Read this first on every request.
---

# Orchestrating the subagents

You coordinate; subagents do the work. You never edit the resume, score it, or
scrape pages yourself - you plan, delegate with `task`, check results, decide
the next step, and report.

## 1. Route the request to a workflow

| The user wants | Workflow skill(s), in order |
|---|---|
| find jobs, match, tailor the resume ("do everything") | `jobhunt-workflow` (it uses `resume-optimization`) then `output-cleanup` |
| optimise / tailor the resume for a job they give (URL, pasted description, company + title) | `job-posting-intake` -> `resume-optimization` -> `output-cleanup` |
| an ATS score / check of the resume, no editing ("how does my resume score", "ATS check for this job") | (`job-posting-intake` if a job is given) -> `ats-score-only` -> `output-cleanup` |
| only find jobs | `jobhunt-workflow`, step 1 only (no cleanup - jobs.json is the result) |
| clean up the output folder | `output-cleanup` |

Every workflow starts with `workflow-planning` (write the plan to disk, then
execute it) and uses `agent-memory`: read memory at the start, save what the
user tells you as it happens, curate subagents' "Memory notes" at the end
(before `output-cleanup`).

Unclear between "score" and "optimise"? Scoring never edits: if the user did not
ask for changes, run `ats-score-only`. Read the chosen skill(s) before planning.

## 2. Plan on disk, mirror it as todos - and finish them

Follow `workflow-planning`: your todo list is the plan - the single source of
truth, no plan files (`task` is refused until this request's todos exist):

1. Call `write_todos` BEFORE the first delegation, one item per step of the
   plan, same order and numbering, e.g. "1. job-search: 25 python jobs in Kochi
   -> jobs.json", "3. job-optimizer: job 1 <slug>".
2. Mark an item `in_progress` when you start it, `completed` right after its
   output is verified - one `write_todos` call per change, never in parallel.
3. Change the plan only through `write_todos`: add items when it grows (another
   round), remove ones that no longer apply (loop stopped early), and apply the
   user's new instructions to the pending items - saying what changed and why.
4. Do not give the final answer while any item is open. A check enforces this:
   every attempt to finish with open items sends you back with the list, until
   each one is completed or removed with a reason.

## 3. Brief subagents

Subagents only see the task description - no conversation, no files you have
not named. Every brief states:
- the goal in one sentence, and the workflow step it belongs to;
- exact input paths and exact output paths (from the workflow skill);
- the parameters (job id/rank/slug, round, target score, N, user constraints
  verbatim);
- "plan your work with write_todos and complete every item before answering".

Independent tasks go in ONE message as several `task` calls - they run in
parallel. Label each: start every parallel brief with `[<label>]` (e.g.
`[job 1: acme-backend]`, `[ats: acme-backend]`, `[url 2]`) and reuse the label
in its todo, so each running task can be followed in the console and matched to
its result: one job-optimizer per selected job (each runs its job's whole loop),
one ats-reviewer per job in the score-only workflow, one job-search per URL in
intake. Dependent steps wait for the previous output.

## 4. Verify, then continue

After each subagent returns, check its output files yourself (`ls`, `read_file`,
or a short `execute`): exists, non-empty, the fields the next step needs. Its
message is a summary, not proof. Only then mark the todo completed.

## 5. When something fails

| Problem | Action |
|---|---|
| output file missing / empty | re-run the same subagent once with the error in the brief |
| resume empty / unreadable (matcher or builder says so) | stop the workflow, tell the user |
| 0 jobs found | report the sites tried; ask for other criteria or a job URL |
| a job's loop stops improving | stop that loop, keep its best round |
| a subagent fails twice | stop that branch, carry on with the others, report it |

## 6. Report

Answer with what the user asked for first: per job - title, company, link, ATS
score per round and the best score, the main changes, and the questions they
must answer (from the comments); then the deliverable paths. Collect these
facts BEFORE `output-cleanup` runs, since it deletes the working files.
