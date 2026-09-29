---
name: ats-score-only
description: Score the user's resume as it is - against specific jobs or as a generic ATS check - with NO edits to the resume; the ats-reviewer scores the original, and the result is a Word report /output/ats/ats_report.docx (score, breakdown, missing skills and keywords, issues with suggested fixes). Use when the user asks for an ATS score or check and did not ask for changes.
---

# ATS score only (no editing)

Nothing is revised: no resume-builder, no loop, no redline. The ats-reviewer
scores the ORIGINAL resume once per job, and you turn the results into one
report the user keeps.

## Steps (one todo each)

1. **Jobs.** If the user named jobs: `job-posting-intake` (URL / pasted text).
   If they asked to score against found jobs: the jobs are in `selected_jobs` of
   `/output/match/match_report.json`. If no job at all: a generic check - a
   single pseudo-job with slug `general` and empty skill/keyword lists (the
   scorer then rates only sections, format and contact data, and says so).
2. **Score.** One `ats-reviewer` task per job, all in ONE message (parallel),
   each brief starting with its label `[ats: <slug>]`.
   Brief: mode "score-only", the resume path `/input/<resume>.docx` (score it as
   is), job id / slug, output folder `/output/ats/<slug>/`, no target.
   Each writes `/output/ats/<slug>/round-1.json`.
3. **Report.** Write and run a script that builds `/output/ats/ats_report.docx`
   from the round files (below). Verify it opens and lists every job.
4. `output-cleanup` (keeps the report, removes the JSON and scripts).
5. Answer: score per job, the 3 biggest gains available for each, and the path.

## Report script (adapt)

```python
# --- ats-report reference ---
import glob, json
from docx import Document

doc = Document()
doc.add_heading("ATS report", level=1)
for path in sorted(glob.glob("/output/ats/*/round-1.json")):
    r = json.load(open(path))
    doc.add_heading(f"{r.get('job_title') or path.split('/')[-2]} - score {r['score']}/100", level=2)
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "Component", "Points"
    for name, points in r["breakdown"].items():
        row = table.add_row().cells
        row[0].text, row[1].text = name.replace("_", " "), "n/a" if points is None else str(points)
    if r.get("missing_required"):
        doc.add_paragraph("Missing required skills: " + ", ".join(r["missing_required"]))
    if r.get("missing_keywords"):
        doc.add_paragraph("Missing keywords: " + ", ".join(r["missing_keywords"]))
    doc.add_heading("Issues and fixes", level=3)
    for issue in r.get("issues", []):
        line = f"[{issue['priority']}] {issue['problem']}"
        if issue.get("quote"):
            line += f' - "{issue["quote"]}"'
        if issue.get("suggestion"):
            line += f" -> {issue['suggestion']}"
        doc.add_paragraph(line, style="List Bullet")
    if r.get("quality_notes"):
        doc.add_paragraph(r["quality_notes"])
doc.save("/output/ats/ats_report.docx")
print("report written")
```
