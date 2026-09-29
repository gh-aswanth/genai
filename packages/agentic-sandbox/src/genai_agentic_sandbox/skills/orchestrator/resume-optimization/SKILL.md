---
name: resume-optimization
description: Tailor the resume for one or more chosen jobs IN PARALLEL - launch one job-optimizer per selected job, all in a single message; each runs that job's whole builder <-> ATS-reviewer loop on its own and returns its best round's redline, review and final .docx - then verify every job's deliverables. Use after jobs are selected (by the matcher or by job-posting-intake).
---

# Resume optimisation: one optimizer per job, all at once

Input: the selected jobs - `selected_jobs` in `/output/match/match_report.json`
(each with rank, id, title, company, slug and its change list). A job given
directly by the user arrives there through `job-posting-intake`.

You do not step the loop yourself. Each job gets its own **job-optimizer**
subagent that runs the whole loop for that job (build -> ATS review -> next
round ... -> finalise). Launched in ONE message, the optimizers run in parallel:
two jobs take about as long as one.

```
orchestrator ── one message ──┬── task job-optimizer (job 1) ── builder/reviewer rounds ── finalise
                              └── task job-optimizer (job 2) ── builder/reviewer rounds ── finalise
```

## Todos

One item per job, all `in_progress` together while their optimizers run, plus
the check:

```
3. [job 1: <slug>] job-optimizer -> /output/resume/1-<slug>/ | done: 3 deliverables + ats_history.json
4. [job 2: <slug>] job-optimizer -> /output/resume/2-<slug>/ | done: 3 deliverables + ats_history.json
5. verify all jobs' deliverables | done: every job has redline, review, final .docx
```

## Launch - one message, one `task` per job, each labelled

Every brief STARTS with its lane label in square brackets - `[job <rank>:
<slug>]`, e.g. `[job 1: acme-backend]` - and the todo for that job uses the same
label. The console shows each running task under its label (colour, start /
finish lines, "N running in parallel", a batch table at the end), so parallel
work stays readable and you can tell which job a result belongs to.

Brief each job-optimizer with: the resume path (`/input/...`), the report path
`/output/match/match_report.json`, the job's id / rank / title / company / slug,
and its folder `/output/resume/<rank>-<slug>/`. Target score, round limit and
minimum gain are already in its instructions - repeat any user override
("stop after round 1") in the brief.

Never launch them one after another: all `task` calls go in the same message.

## Verify

When the optimizers return, check every job folder yourself:

```
/output/resume/<rank>-<slug>/<stem>_<slug>_redline.docx   colour-coded changes
/output/resume/<rank>-<slug>/<stem>_<slug>_review.docx    Word tracked changes
/output/resume/<rank>-<slug>/<stem>_<slug>_final.docx     clean, ATS-ready
/output/resume/<rank>-<slug>/ats_history.json             scores per round, best round
```

Read each `ats_history.json` for your report (cleanup removes it later). A job
whose optimizer failed or left deliverables missing: re-launch that ONE job's
optimizer (with the error in the brief); the finished jobs stay as they are.
