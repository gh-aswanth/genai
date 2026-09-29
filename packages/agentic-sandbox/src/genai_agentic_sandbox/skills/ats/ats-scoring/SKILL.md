---
name: ats-scoring
description: Score a resume (.docx) against one job - or as a generic check - the way an ATS parses it - keyword and required-skill coverage, standard sections, parse-breaking formatting, contact data, dates - with a fixed, reproducible scoring script, then add a recruiter-style quality review and write actionable feedback (exact quotes + suggested fixes) for the resume builder's next round. Use for every ATS review round.
---

# ATS scoring round

You are the independent reviewer in a loop: the resume builder revises, you
score and give feedback, it revises again - until the score reaches the target,
stops improving, or the round limit is hit. Judge the resume, not the builder.

## Two modes (your brief says which)

| | mode "loop" (resume optimisation) | mode "score-only" (no editing) |
|---|---|---|
| score | `v<k>/<stem>_<slug>_final.docx` of round k | the user's resume as is, `/input/<resume>.docx` |
| folder `<F>` | `/output/resume/<rank>-<slug>/` | `/output/ats/<slug>/` |
| output | `<F>/ats/round-<k>.json` | `<F>/round-1.json` |
| verdict | `done` / `improve` against the target | `n/a` (nobody revises) |

Below, `<ATS>` means `<F>/ats/` in loop mode and `<F>` in score-only mode.

## Inputs (from your brief)

- the mode, the folder, the round `k` (score-only: always 1), the target score
  (loop mode)
- the job: its `id` in `/output/jobs/jobs.json` and its entry in
  `/output/match/match_report.json` -> `selected_jobs` (keywords, missing
  skills). Generic check (slug `general`): no job - empty skill and keyword
  lists; those components are then "n/a" and the score is out of what remains.

## 1. Deterministic score - the same script every round

Round 1: write `<ATS>ats_score.py` from the reference below (you may extend the
keyword list from the posting, but not the weights). Rounds 2+: run the SAME
script unchanged, so scores are comparable.

```
python <ATS>ats_score.py <resume.docx> <ATS>job.json <ATS>score-<k>.json
```

(`job.json`: write the job's record once, with `title`, `company`,
`required_skills`, `nice_to_have` and a `keywords` list of the posting's
important terms.)

```python
# --- ats-score reference ---
import json, re, sys
from docx import Document
from docx.oxml.ns import qn

WEIGHTS = {"required_skills": 35, "keywords": 20, "sections": 15, "format": 15,
           "contact": 10, "nice_to_have": 5}
SECTIONS = {
    "summary": ("summary", "profile", "objective", "about me", "professional summary"),
    "experience": ("experience", "employment", "work history", "professional experience"),
    "skills": ("skills", "technical skills", "core competencies", "technologies"),
    "education": ("education", "qualifications", "academic"),
}
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE = re.compile(r"(?:\+?\d[\d\s().-]{8,}\d)")
LINKEDIN = re.compile(r"linkedin\.com/\S+", re.I)


def extract(path):
    doc = Document(path)
    body = doc.element.body
    paras = ["".join(t.text or "" for t in p.iter(qn("w:t"))) for p in body.iter(qn("w:p"))]
    header_footer = " ".join(
        p.text for s in doc.sections for part in (s.header, s.footer) for p in part.paragraphs
    )
    return {
        "paragraphs": paras,
        "text": "\n".join(paras),
        "tables": len(body.findall(qn("w:tbl"))),
        "text_boxes": len(body.xpath(".//w:txbxContent")),
        "images": len(body.xpath(".//w:drawing | .//w:pict")),
        "columns": max([int(c.get(qn("w:num"), "1")) for c in body.iter(qn("w:cols"))] or [1]),
        "header_footer_text": header_footer.strip(),
        "tracked_changes": len(body.xpath(".//w:ins | .//w:del")),
        "words": len(re.findall(r"\w+", "\n".join(paras))),
    }


def norm(s):
    return " " + re.sub(r"[^a-z0-9+#]+", " ", s.lower()).strip() + " "


def has(text, term):
    t = norm(term).strip()
    return bool(t) and f" {t} " in norm(text)


def coverage(text, terms):
    """(share found or None when there is nothing to look for, found, missing)."""
    terms = [t for t in dict.fromkeys(terms or []) if t.strip()]
    hit = [t for t in terms if has(text, t)]
    return (len(hit) / len(terms) if terms else None), hit, [t for t in terms if t not in hit]


def score(resume_path, job):
    f = extract(resume_path)
    text, issues = f["text"], []
    req, req_hit, req_miss = coverage(text, job.get("required_skills"))
    kw, kw_hit, kw_miss = coverage(text, job.get("keywords"))
    nice, _, nice_miss = coverage(text, job.get("nice_to_have"))
    heads = {
        name: any(len(p) <= 40 and any(has(p, a) for a in aliases) for p in f["paragraphs"])
        for name, aliases in SECTIONS.items()
    }
    fmt, fmt_notes = 1.0, []
    for key, penalty, note in (
        ("tables", 0.3, "content in tables - many ATS read tables out of order or skip them"),
        ("text_boxes", 0.3, "text boxes - ATS usually skip their text"),
        ("images", 0.1, "images/graphics - ignored by ATS"),
        ("tracked_changes", 0.3, "unaccepted tracked changes left in the file"),
    ):
        if f[key]:
            fmt -= penalty
            fmt_notes.append(note)
    if f["columns"] > 1:
        fmt -= 0.3
        fmt_notes.append("multi-column layout - parsed out of order")
    if EMAIL.search(f["header_footer_text"]) or PHONE.search(f["header_footer_text"]):
        fmt -= 0.2
        fmt_notes.append("contact details in header/footer - often not parsed")
    if not 250 <= f["words"] <= 1100:
        fmt -= 0.1
        fmt_notes.append(f"length {f['words']} words - aim for 350-900")
    fmt = max(fmt, 0.0)
    contact_found = {"email": bool(EMAIL.search(text)), "phone": bool(PHONE.search(text)),
                     "linkedin": bool(LINKEDIN.search(text))}
    parts = {
        "required_skills": req, "keywords": kw, "nice_to_have": nice,
        "sections": sum(heads.values()) / len(heads), "format": fmt,
        "contact": sum(contact_found.values()) / len(contact_found),
    }
    # A component with nothing to measure (no job given) is "n/a" (None) and the
    # score is scaled to 100 over the components that apply.
    breakdown = {k: None if v is None else round(WEIGHTS[k] * v, 1) for k, v in parts.items()}
    applicable = sum(WEIGHTS[k] for k, v in parts.items() if v is not None)
    total = 100 * sum(v for v in breakdown.values() if v is not None) / applicable
    for s in req_miss:
        issues.append({"priority": "high", "category": "required_skill", "quote": None,
                       "problem": f"required skill '{s}' not found", "suggestion": None})
    for s in kw_miss:
        issues.append({"priority": "medium", "category": "keyword", "quote": None,
                       "problem": f"posting keyword '{s}' not found", "suggestion": None})
    for name, ok in heads.items():
        if not ok:
            issues.append({"priority": "high", "category": "section", "quote": None,
                           "problem": f"no standard '{name}' heading", "suggestion": None})
    for note in fmt_notes:
        issues.append({"priority": "high", "category": "format", "quote": None,
                       "problem": note, "suggestion": None})
    for key, ok in contact_found.items():
        if not ok:
            issues.append({"priority": "medium", "category": "contact", "quote": None,
                           "problem": f"no {key} in the body text", "suggestion": None})
    return {
        "score": round(total, 1),
        "breakdown": breakdown,
        "job_title": job.get("title"), "company": job.get("company"),
        "matched_required": req_hit, "missing_required": req_miss,
        "matched_keywords": kw_hit, "missing_keywords": kw_miss,
        "missing_nice_to_have": nice_miss,
        "sections": heads, "contact": contact_found,
        "format": {k: f[k] for k in ("tables", "text_boxes", "images", "columns", "words", "tracked_changes")},
        "issues": issues,
    }


if __name__ == "__main__":
    resume, job_file, out = sys.argv[1:4]
    result = score(resume, json.load(open(job_file)))
    json.dump(result, open(out, "w"), indent=2)
    print(f"score {result['score']}", result["breakdown"])
    print("job:", result["job_title"], "@", result["company"])
    print("missing required:", result["missing_required"])
    print("missing keywords:", result["missing_keywords"][:15])
```

## 2. Quality review (your judgement, on top of the script)

Read the final copy's text and add issues the script cannot see. Every issue
quotes the resume **exactly** (`quote`) so the builder can find it, and says
what to do (`suggestion`):

- **wrong data**: overlapping or backwards dates, end dates in the future,
  typos, misspelled technologies, inconsistent job titles or company names
  (category `wrong_data`, priority high, suggestion = the correction)
- weak bullets: no action verb, no result, no number (category `bullet`)
- keywords the experience supports but never names (category `keyword`)
- a missing Summary/Skills that the resume's own content could fill
  (category `missing_info`) - facts not in the resume are `ask_user`
- anything that still reads generic for THIS job

Quality issues do not change the script's `score` (keep it reproducible);
report them in `issues` and a short `quality_notes`.

## 3. Verdict and output

Write `<ATS>round-<k>.json` (loop: `/output/resume/<rank>-<slug>/ats/round-<k>.json`;
score-only: `/output/ats/<slug>/round-1.json`):

```json
{
  "round": 2,
  "mode": "loop",
  "job_title": "Senior Backend Engineer", "company": "Acme",
  "resume": "/output/resume/1-acme-backend/v2/cv_acme-backend_final.docx",
  "score": 86.5,
  "previous_score": 78.0,
  "breakdown": {"required_skills": 35.0, "keywords": 16.0, "sections": 15.0, "format": 10.5, "contact": 6.7, "nice_to_have": 3.3},
  "missing_required": [],
  "missing_keywords": ["microservices"],
  "issues": [
    {"priority": "high", "category": "wrong_data", "quote": "2020–2025",
     "problem": "end date in the future for a finished role", "suggestion": "2020–Present"}
  ],
  "quality_notes": "one paragraph",
  "verdict": "improve | done",
  "reason": "why this verdict"
}
```

Loop mode: `verdict` is `done` when `score` >= the target in your brief AND no
high priority issue remains that the builder can fix from the resume's own
facts; otherwise `improve`. Score-only mode: `verdict` is `n/a`, and
`quality_notes` says what would raise the score most. Order `issues` by priority. Keep `ats_score.py`, `job.json` and every
`round-<k>.json` - the orchestrator reads them (its cleanup removes them at the
end).
