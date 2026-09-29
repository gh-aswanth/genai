---
name: output-cleanup
description: Remove every agent-generated working file from /output (scripts, JSON, markdown, notes, per-round folders, intermediate documents) and keep only the deliverable .docx files - the tailored resumes' redline/review/final copies and the ATS report - with a dry run first. Use as the last step of a workflow (after collecting the facts for the report), or when the user asks for a cleanup.
---

# Output cleanup

The user keeps documents, not working files. At the end of a workflow, after
you have collected everything your final answer needs (scores, questions,
changes), remove the rest.

**Skip this skill** if the run settings say to keep working files, or the user
asked to keep them, or the workflow was "only find jobs".

## What stays

| Kept | Why |
|---|---|
| `/output/resume/<rank>-<slug>/<stem>_<slug>_{redline,review,final}.docx` | the tailored resumes (top of each job folder only) |
| `/output/ats/ats_report.docx` | the ATS-only report |
| `/output/original/` | the user's resume copy (read-only) |
| `/output/memories/` | long-term memory (also mounted at `/memories`) - never deleted |
| `/output/.browser/profile/` | the browser's profile (cookies, in use) - captures next to it ARE deleted |

Everything else under `/output` is deleted - including the `v<k>/` round
folders, `ats/` feedback, scripts, `jobs.json`, `match_report.*`, notes,
change logs - and emptied folders are removed.

## Steps (one todo each)

1. Write the script below to `/tmp/cleanup_output.py` (NOT under /output) and
   run it **without** `--apply`: it lists what it would keep and delete.
2. Check the list: every deliverable of this run is in KEEP. If not, stop and
   fix the cause (e.g. the best round was never copied up) before cleaning.
3. Run it with `--apply`. Confirm the printed KEEP list in your answer.

```python
# --- cleanup reference ---
import os, re, shutil, sys
from pathlib import Path

ROOT = Path("/output")
# folders never touched (relative to /output)
PROTECTED = {"original", "memories", ".browser/profile"}
DELIVERABLE = [
    # (?!~\$): Word's lock files ("~$name.docx", while a file is open) are not deliverables
    re.compile(r"^resume/[^/]+/(?!~\$)[^/]+_(redline|review|final)\.docx$"),
    re.compile(r"^ats/ats_report\.docx$"),
]


def plan(root=ROOT):
    keep, delete = [], []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = [d for d in dirnames if (rel_dir / d).as_posix() not in PROTECTED]
        for name in filenames:
            rel = (rel_dir / name).as_posix()
            (keep if any(p.match(rel) for p in DELIVERABLE) else delete).append(rel)
    return sorted(keep), sorted(delete)


def apply(delete, root=ROOT):
    for rel in delete:
        path = root / rel
        if path.is_symlink() or path.is_file():
            path.unlink()
    for dirpath, _dirs, _files in sorted(os.walk(root), key=lambda x: -len(x[0])):
        d = Path(dirpath)
        rel = d.relative_to(root)
        protected = any(rel.as_posix() == p or rel.as_posix().startswith(p + "/") for p in PROTECTED)
        if d != root and not protected and not any(d.iterdir()):
            d.rmdir()


if __name__ == "__main__":
    root = Path(sys.argv[sys.argv.index("--root") + 1]) if "--root" in sys.argv else ROOT
    keep, delete = plan(root)
    print("KEEP:", *keep, sep="\n  ")
    print(f"DELETE ({len(delete)} files):", *delete[:200], sep="\n  ")
    if "--apply" in sys.argv:
        apply(delete, root)
        print("deleted", len(delete), "files")
    else:
        print("dry run - re-run with --apply")
```
