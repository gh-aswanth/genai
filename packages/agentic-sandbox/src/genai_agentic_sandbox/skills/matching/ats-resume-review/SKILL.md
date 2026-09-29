---
name: ats-resume-review
description: Score a resume against job postings the way an ATS and a recruiter would - keyword coverage, required vs nice-to-have skills, experience fit, ATS formatting problems - and produce a concrete, prioritised change list in /output/match/match_report.json. Use when matching a resume to jobs.
---

# ATS resume review and job matching

## Inputs

- Resume: `.docx` under `/input/`. Read it with a script you write and run
  (python-docx). Number paragraphs over `doc.element.body.iter(qn("w:p"))` so
  table cells are included - the resume builder uses the same indexes:
  ```python
  from docx import Document
  from docx.oxml.ns import qn
  doc = Document("/input/cv.docx")
  for i, p in enumerate(doc.element.body.iter(qn("w:p"))):
      style = p.xpath("string(./w:pPr/w:pStyle/@w:val)") or "Normal"
      in_table = bool(p.xpath("ancestor::w:tbl"))
      print(i, style, in_table, "".join(t.text or "" for t in p.iter(qn("w:t"))))
  ```
- Jobs: `/output/jobs/jobs.json` (array; see `required_skills`, `description`).

## 1. Build the resume profile

From the resume text only: skills (normalise synonyms - "Postgres" = PostgreSQL,
"JS" = JavaScript, "k8s" = Kubernetes), total years of experience, titles,
domains, education, certifications, and quantified achievements. Record the
paragraph index where each item appears.

Do the counting in code: load `jobs.json` with pandas, normalise skill names
with a synonym map, and compute coverage per job - save the script as
`/output/match/score_jobs.py` so the numbers are reproducible.

## 2. Score every job (0-100)

| Component            | Weight | How                                                        |
|----------------------|--------|------------------------------------------------------------|
| Required skills      | 45     | share of the job's required skills present in the resume   |
| Title / seniority    | 15     | resume titles and years vs the posting                     |
| Keyword coverage     | 20     | important nouns/phrases from the description present verbatim |
| Nice-to-have skills  | 10     | share present                                              |
| Domain / location    | 10     | industry, location / remote fit                            |

Be literal, like an ATS: a skill the resume only implies does not count as
present, but note it as "implied - make explicit". Never assume skills.

## 3. ATS formatting check (whole resume)

Flag, with paragraph indexes: contact details inside tables/headers (many ATS
skip them), missing standard section headings (Summary, Skills, Experience,
Education), dates in inconsistent formats, graphics/text boxes, very long
paragraphs instead of bullets, bullets without outcomes or numbers, first-person
pronouns, acronyms never spelled out, missing LinkedIn/GitHub.

## 4. Select the best N jobs and write a change list for EACH

N comes from your task (default 2). Pick the N highest-scoring jobs, breaking
ties by fewer missing required skills. Give each a `rank` (1..N) and a `slug`
for its folder: lowercase, `a-z0-9-`, company + role, max 40 chars (e.g.
`cubet-lead-python-engineer`).

For each selected job, write ITS OWN change list - what this resume needs for
this posting, not an average across jobs. Each change:

```json
{
  "id": "J1-C1",
  "priority": "high | medium | low",
  "type": "add_keyword | rephrase | quantify | reorder | remove | format | add_section | confirm_skill",
  "paragraph": 12,
  "current_text": "exact text from the resume, or null for additions",
  "suggested_text": "exact new wording, or null",
  "reason": "why - e.g. 'posting requires PostgreSQL; resume says SQL'"
}
```

`paragraph` and `current_text` must come from your dump of the resume (same
indexing the builder uses). Use `confirm_skill` for a skill the job requires that
the resume does not show: the builder only comments on it, never claims it.
High = a required skill/keyword of this job or an ATS parse blocker.

## 5. Output

`/output/match/match_report.json`:

```json
{
  "resume": "/input/resume.docx",
  "resume_paragraphs": 59,
  "profile": {"skills": [], "years_experience": 5, "titles": []},
  "jobs": [{"id": "", "title": "", "company": "", "url": "", "score": 82,
            "matched_required": [], "missing_required": [], "matched_nice": [],
            "missing_keywords": [], "fit_summary": "one sentence"}],
  "selected_jobs": [
    {"rank": 1, "id": "", "title": "", "company": "", "url": "", "slug": "",
     "score": 82, "missing_required": [], "keywords_to_add": [],
     "changes": []}
  ],
  "ats_issues": [{"paragraph": 0, "issue": "", "fix": ""}],
  "required_skills_summary": [{"skill": "PostgreSQL", "required_by": 7, "in_resume": false}]
}
```

`jobs` sorted by score, descending. `selected_jobs` has exactly N entries (fewer
only if fewer jobs exist). Also write `/output/match/match_report.md`: a table of
the top jobs (title, company, score, missing required skills, link), the selected
jobs with their change lists, the skills the market asks for vs the resume, and
the ATS issues.

## Empty or unreadable resume

If your dump shows under ~200 characters of text, STOP: write
`"resume_status": "empty"` in the report, say so in your reply, and do not
select jobs or write changes. The orchestrator will stop the pipeline.
