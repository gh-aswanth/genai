---
name: job-optimization-loop
description: Run ONE job's whole resume-tailoring loop as a job optimizer - resume-builder round, ats-reviewer score, repeat until the target score / no more gain / round limit - then copy the best round's redline, review and final .docx into the job folder and write ats_history.json. Other jobs run in parallel in their own optimizers; stay inside your job's folder.
---

# One job, the whole loop

You own exactly one job (from your brief: id, rank, title, company, slug) and its
folder `/output/resume/<rank>-<slug>/`. Other job optimizers run at the same
time on other jobs - never read or write their folders. Shared inputs are
read-only: the resume (`/input/...`) and `/output/match/match_report.json`.

## Todos (your plan)

Start with `write_todos` (on its own):

```
1. resume-builder: round 1 -> v1/ | done: 3 .docx, all CHECK lines True
2. ats-reviewer: round 1 -> ats/round-1.json | done: score + verdict
3. finalise: best round up + ats_history.json | done: 3 deliverables in the job folder
```

After each review either add "resume-builder: round k+1" and "ats-reviewer:
round k+1" before "finalise", or go to finalise.

## The loop

```
k = 1
loop:
    resume-builder(job, round k, feedback = /output/resume/<rank>-<slug>/ats/round-<k-1>.json if k > 1)
        -> /output/resume/<rank>-<slug>/v<k>/<stem>_<slug>_{redline,review,final}.docx
    ats-reviewer(mode "loop", job, round k, target, that round's _final.docx)
        -> /output/resume/<rank>-<slug>/ats/round-<k>.json   (score, issues, verdict)
    read_file the round file; stop if
         verdict == "done"
      or score >= target
      or k > 1 and score - previous_score < minimum gain
      or k == max rounds
    k += 1
best = the round with the highest score (ties: the later round)
```

A round's build and review are sequential (the review scores the build). The
target, round limit and minimum gain are in your instructions.

## Briefs

- **resume-builder**: resume path, `/output/match/match_report.json`, job id /
  rank / title / company / slug, round k, folder `/output/resume/<rank>-<slug>/`,
  and from round 2 the feedback file `/output/resume/<rank>-<slug>/ats/round-<k-1>.json`.
- **ats-reviewer**: mode "loop", job id / rank / slug, round k, target score, the
  round's `v<k>/<stem>_<slug>_final.docx`, the job folder.

If a subagent fails twice on the same round, stop the loop, finalise with the
best round so far, and say so.

## Finalise

Copy the best round's three files up into the job folder and record the history:

```python
import json, shutil, glob, os
job = "/output/resume/1-acme-backend"
rounds = [json.load(open(f)) for f in sorted(glob.glob(f"{job}/ats/round-*.json"))]
best = max(rounds, key=lambda r: (r["score"], r["round"]))["round"]
for f in glob.glob(f"{job}/v{best}/*.docx"):
    if not os.path.basename(f).startswith("~$"):  # skip Word lock files
        shutil.copy(f, job)
json.dump({"best_round": best, "rounds": [{k: r[k] for k in ("round", "score", "verdict")} for r in rounds]},
          open(f"{job}/ats_fhistory.json", "w"), indent=2)
print("best", best, [r["score"] for r in rounds])
```

Deliverables (what the user keeps after cleanup):

```
/output/resume/<rank>-<slug>/<stem>_<slug>_redline.docx   colour-coded changes
/output/resume/<rank>-<slug>/<stem>_<slug>_review.docx    Word tracked changes
/output/resume/<rank>-<slug>/<stem>_<slug>_final.docx     clean, ATS-ready
```

Answer with the job, the score per round, the best round, the three paths, the
main changes and the questions for the user (from the builder's comments).
