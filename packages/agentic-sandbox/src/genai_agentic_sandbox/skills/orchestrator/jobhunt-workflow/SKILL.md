---
name: jobhunt-workflow
description: The full job hunt - search the web for jobs, match them against the resume and pick the best N, then tailor the resume for each through the resume-optimization loop, and clean up. The file contract between job-search, job-matcher, resume-builder and ats-reviewer. Use when the user wants jobs found and the resume tailored.
---

# Full job hunt

## Steps (one todo each; step 3 adds todos per job and round)

| Step | Subagent | Reads | Writes |
|------|----------|-------|--------|
| 1 | `job-search` | the user's criteria | `/output/jobs/jobs.json`, `/output/jobs/search_notes.md` |
| 2 | `job-matcher` | resume + `jobs.json` | `/output/match/match_report.json` (with `selected_jobs`), `.md` |
| 3 | loop: `resume-builder` + `ats-reviewer` | per selected job | see `resume-optimization` |
| 4 | you | everything | the final report (collect facts), then `output-cleanup` |

## Briefs

- **job-search**: role/keywords, location / remote, seniority, sites or URLs
  (default: the web-job-search skill's list), how many jobs (default 25), dates.
  Output `/output/jobs/jobs.json`.
- **job-matcher**: resume path, `/output/jobs/jobs.json`, N (from your run
  parameters), target roles. Output `/output/match/match_report.json` with
  exactly N `selected_jobs` (rank, id, title, company, url, slug, changes).
- **step 3**: follow `resume-optimization` with the N selected jobs.

## Stop conditions

- The matcher reports the resume empty/unreadable: stop, tell the user.
- job-search finds 0 jobs: stop, report the sites tried.
- "Only find jobs": stop after step 1, report the jobs (no cleanup).

## Contract recap

```
/output/jobs/jobs.json            [{id, title, company, location, url, description,
                                    required_skills, nice_to_have, ...}]
/output/match/match_report.json   {jobs: [...scored...], selected_jobs: [{rank, id, slug, changes}], ...}
/output/resume/<rank>-<slug>/     per selected job (resume-optimization)
```
