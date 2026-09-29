---
name: resume-optimization
description: Tailor the resume for one or more chosen jobs through the builder <-> ATS-reviewer loop - each round the resume-builder writes colour-coded redline, tracked review and clean final copies, the ats-reviewer scores the final copy and gives feedback, until the target score is reached or the score stops improving - then keep the best round per job. Use after jobs are selected (by the matcher or by job-posting-intake).
---

# Resume optimisation loop

Input: the selected jobs - `selected_jobs` in `/output/match/match_report.json`
(each with rank, id, slug and its change list). A job given directly by the user
arrives there through `job-posting-intake`. Parameters come from your run
settings: target score, max rounds, minimum gain per round (2 points).

## The loop, per job

```
k = 1
loop:
    resume-builder(job, round k, feedback = ats/round-<k-1>.json if k > 1)
        -> /output/resume/<rank>-<slug>/v<k>/<stem>_<slug>_{redline,review,final}.docx
    ats-reviewer(job, round k, target, v<k>/<stem>_<slug>_final.docx)
        -> /output/resume/<rank>-<slug>/ats/round-<k>.json   (score, issues, verdict)
    read_file the round file; stop if
         verdict == "done"
      or score >= target
      or k > 1 and score - previous_score < minimum gain
      or k == max rounds
    k += 1
best = the round with the highest score (ties: the later round)
```

Jobs run side by side: at each step send one `task` per still-running job in the
SAME message. A job whose loop stopped drops out; the others continue.

## Todos

Before starting: one todo per job and round-1 step ("job 1 round 1 build",
"job 1 round 1 ATS review", same for job 2), plus "finalise job 1/2". After each
review, either add the next round's two todos or mark the loop finished.

## Briefs

- **resume-builder**: resume path (`/input/...`), `/output/match/match_report.json`,
  job id / rank / title / company / slug, round k, folder
  `/output/resume/<rank>-<slug>/`, and from round 2 the feedback file
  `/output/resume/<rank>-<slug>/ats/round-<k-1>.json`.
- **ats-reviewer**: mode "loop", job id / rank / slug, round k, target score,
  the round's `v<k>/<stem>_<slug>_final.docx`, the job folder.

## Finalise each job

When a job's loop ends, copy the best round's three files up into the job
folder, and record the history (keep the facts for your report - cleanup
removes the JSON later):

```python
import json, shutil, glob
job = "/output/resume/1-acme-backend"
rounds = [json.load(open(f)) for f in sorted(glob.glob(f"{job}/ats/round-*.json"))]
best = max(rounds, key=lambda r: (r["score"], r["round"]))["round"]
for f in glob.glob(f"{job}/v{best}/*.docx"):
    shutil.copy(f, job)
json.dump({"best_round": best, "rounds": [{k: r[k] for k in ("round", "score", "verdict")} for r in rounds]},
          open(f"{job}/ats_history.json", "w"), indent=2)
print("best", best, [r["score"] for r in rounds])
```

Deliverables per job (what the user keeps after cleanup):

```
/output/resume/<rank>-<slug>/<stem>_<slug>_redline.docx   colour-coded changes
/output/resume/<rank>-<slug>/<stem>_<slug>_review.docx    Word tracked changes
/output/resume/<rank>-<slug>/<stem>_<slug>_final.docx     clean, ATS-ready
```
