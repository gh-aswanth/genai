---
name: job-posting-intake
description: Turn a job the user gives - a URL, a pasted job description, or a company + title - into a normalised job record in /output/jobs/jobs.json and a selected_jobs entry in /output/match/match_report.json, so the resume-optimization or ats-score-only workflow can run on it without a web search. Use whenever the user names a specific job.
---

# Job posting intake

Goal: the same files the search + match steps would produce, for jobs the user
chose themselves.

## 1. Get the posting text

| User gave | Do |
|---|---|
| a URL | `job-search` subagent, brief: "open this URL only, extract this one posting (detail page), save it as the only record of /output/jobs/jobs.json" |
| pasted description | write it yourself: `write_file /output/jobs/posting-<n>.txt`, then a script builds the record |
| company + title only | `job-search`: find that exact posting (company careers page first) |

Several jobs: one record each (several URLs -> one job-search task per URL, in
parallel, each writing `/output/jobs/job-<n>.json`; then merge into
`jobs.json` with a script).

## 2. Normalise (script, run with `execute`)

Each record follows the job-search schema: `id`, `title`, `company`,
`location`, `url`, `description`, `required_skills`, `nice_to_have`, `source`
(`"user"`). Extract `required_skills` / `nice_to_have` from the description
text only; unknown fields are `null`.

## 3. Match and select

Brief `job-matcher` with the resume path, `/output/jobs/jobs.json` and
N = number of user-given jobs: it scores them and writes `selected_jobs` (all of
them, ranked) with per-job change lists and slugs. Then continue with
`resume-optimization` or `ats-score-only`.
