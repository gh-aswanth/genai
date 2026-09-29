---
name: docx-tracked-revisions
description: Build a tailored, ATS-optimised copy of a .docx resume for one job by writing and running your own Python (python-docx + lxml) - colour-coded Word tracked changes (added green, removed red, rephrased blue, moved purple, styling yellow, wrong data flagged) with comments, plus a clean final copy - and improve it round by round from ATS reviewer feedback. Use whenever editing the resume.
---

# Tailored resume in review mode (code you write and run)

There is no revision tool. You write a Python script, run it with `execute`,
read what it prints, fix, re-run. The sandbox has Python 3.14 with `python-docx`
and `lxml` (no network). Word shows a change as tracked only if the XML says so,
so the script edits the XML with the patterns below.

## What you produce, per round

Your brief names the job (rank, slug, id), the round `k`, and - from round 2 -
the ATS feedback file of the previous round. Everything goes in the job folder
`/output/resume/<rank>-<slug>/`, and your script is always
`/output/resume/<rank>-<slug>/revise_resume.py`:

| File | What |
|---|---|
| `revise_resume.py` | your script; you extend it each round |
| `v<k>/<stem>_<slug>_redline.docx` | **what the user reads**: colour-coded visual diff (removed text still visible, struck through) + comments; looks the same in every viewer |
| `v<k>/<stem>_<slug>_review.docx` | the same changes as Word tracked changes + comments, to Accept / Reject one by one |
| `v<k>/<stem>_<slug>_final.docx` | same changes applied, no colours, no comments - what the ATS reviewer scores and the user sends |
| `change_log.md` | cumulative: each change, why, which job requirement / feedback item, open questions |

Every round rebuilds ALL THREE files **from the original input** with the full,
cumulative set of changes (round 1's changes + fixes for round 2's feedback...).
Never edit a previous version: the review copy must always show all changes
against the user's own resume, and Reject All must give it back exactly.

## Colours (the user reads the review copy by colour)

| Kind | Colour | Use for |
|---|---|---|
| `added` | green text | new words, new bullet points, new sections, filled-in missing information |
| `removed` | red text, struck through (tracked delete) | removed words / paragraphs, and the OLD side of a replacement |
| `rephrased` | blue text | the NEW wording of a replacement / rewrite |
| `moved` | purple text | moved sections, reordered bullet points (both ends of the move) |
| `styled` | yellow shading | formatting / paragraph-style changes (heading styles, bold, list style) |
| `flag` | dark red on pink, double underline | suspected wrong data (date conflicts, typos, wrong tech names, inconsistent titles) - with a comment giving the alternative |

Word and LibreOffice paint tracked insertions/deletions in the *reviewer's*
markup colour, which hides text colour - that is why the redline copy exists:
it carries the colours as plain formatting (struck-through red for removed text,
green for added, ...). In the tracked review copy, colour on text that already
existed is itself a tracked formatting change (`w:rPrChange`), so Reject All
restores the original exactly. The first comment in both is the colour legend
(`add_legend`).

## Workflow

1. **Understand the file** (round 1). Dump it:
   ```python
   from docx import Document
   from docx.oxml.ns import qn
   doc = Document("/input/cv.docx")
   for i, p in enumerate(doc.element.body.iter(qn("w:p"))):   # table cells included
       style = p.xpath("string(./w:pPr/w:pStyle/@w:val)") or "Normal"
       runs = p.xpath("./w:r | ./w:hyperlink/w:r")
       text = "".join(t.text or "" for t in p.iter(qn("w:t")))
       print(f"[{i}] ({style}, {len(runs)} runs) {text!r}")
   ```
   Note tables, text boxes, headers/footers, fields (`w:fldChar`), hyperlinks,
   existing revisions, and which styles exist (`[s.style_id for s in doc.styles]`).
   **Under ~200 characters of text: stop and report the resume as empty - never
   build a skeleton or placeholder resume.**
2. **Plan the changes** for this job - from its `selected_jobs` entry in
   `/output/match/match_report.json` (round 1) plus, from round 2, every issue in
   the ATS feedback file. Cover, in this order:
   - **Wrong data**: dates that overlap or run backwards, typos, misspelled
     technologies, inconsistent titles. Certain fix -> `track_replace` + comment;
     unsure -> `flag_wrong` with the alternative.
   - **Missing information** the ATS/job needs that the resume already supports:
     a Summary written from the user's own experience, a consolidated Skills line
     of skills the resume already shows, standard headings, missing role
     keywords the experience demonstrates. Add with `insert_paragraph_after` /
     `track_insert` (green).
   - **ATS format**: standard section names (Summary, Skills, Experience,
     Education, Certifications) via `track_replace`; heading and bullet styles
     via `track_paragraph_style` (yellow); most relevant section/bullets first via
     `move_paragraph_after` (purple); remove noise via `delete_paragraph` (red).
     Content locked in tables/text boxes/headers: comment that the ATS may skip it.
   - **Rephrasing** bullets for this job: action verb + what + measurable result,
     keywords of the posting - only with facts already in the resume.
   Facts you cannot derive from the resume (a LinkedIn URL, a certification date,
   a skill the user never mentions) are NOT invented: add a comment asking for it.
3. **Write / extend** `revise_resume.py` with a `build(mode, out)` function
   (example below) and run it: it writes the review, redline and final copies of
   round k.
4. **Verify** (the script prints this):
   - review, all changes rejected == original text, same paragraph count;
   - review, all changes accepted == final copy's text;
   - final copy has no revision markup, comments or review colours;
   - every change printed OK (fix every FAIL and re-run).
5. Update `change_log.md` (round k section: changes, feedback items addressed,
   questions for the user).

## The markup

| Change | XML |
|---|---|
| insertion | `<w:ins w:id w:author w:date><w:r>..<w:t>new</w:t></w:r></w:ins>` |
| deletion | `<w:del ...><w:r>..<w:delText>old</w:delText></w:r></w:del>` (field codes: `w:delInstrText`) |
| replacement | a `w:del` immediately followed by a `w:ins` |
| run formatting | new `w:rPr` properties + LAST child `<w:rPrChange ...><w:rPr>OLD</w:rPr></w:rPrChange>` |
| paragraph style | new `w:pPr` + LAST child `<w:pPrChange ...><w:pPr>OLD</w:pPr></w:pPrChange>` |
| new / deleted paragraph | runs in `w:ins` / `w:del`, and the same flag inside `w:pPr/w:rPr` (the paragraph mark) |
| move | source: `w:moveFromRangeStart w:name=moveN`, runs in `w:moveFrom`, `w:moveFromRangeEnd`, mark `w:moveFrom`; destination: same with `moveTo`, same name |
| comment | `w:commentRangeStart` .. `w:commentRangeEnd` + run with `w:commentReference`, same id as in `comments.xml` |
| review mode on | `<w:trackRevisions/>` in `settings.xml` |

Every revision element needs a unique `w:id` (above any id already in the
file), `w:author` and `w:date` (ISO, UTC). Text is often split across runs:
split at exact offsets before wrapping, and copy the neighbouring run's `w:rPr`
into new runs so inserted text keeps the font.

## Helper patterns

Adapt these in your script; they are the parts that are easy to get wrong.

```python
# --- tracked-change helpers ---
import copy, datetime as dt, itertools
from docx import Document
from docx.enum.text import WD_UNDERLINE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import RGBColor
from docx.text.run import Run

AUTHOR, INITIALS = "JobHunter AI", "JH"
DATE = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
W_T, W_TAB, W_RPR, W_PPR = qn("w:t"), qn("w:tab"), qn("w:rPr"), qn("w:pPr")
REVIEW = True  # False: build the clean final copy (no colours, no comments, all accepted)
_ids = itertools.count(1)

# kind: (text colour, shading fill)
PALETTE = {
    "added": ("00B050", None),
    "removed": ("FF0000", None),
    "rephrased": ("0070C0", None),
    "moved": ("7030A0", None),
    "styled": (None, "FFF2CC"),
    "flag": ("C00000", "FFC7CE"),
}
LEGEND = (
    "Review colours - green: added | red, struck: removed / replaced (old text) | "
    "blue: rephrased (new text) | purple: moved or reordered | yellow shading: formatting "
    "or style change | dark red on pink, double underline: suspected wrong data (see the "
    "comment for the alternative). Reject All restores your original; the *_final.docx "
    "has every change applied and no colours."
)
_AFTER_SHD = ("w:fitText", "w:vertAlign", "w:rtl", "w:cs", "w:em", "w:lang",
              "w:eastAsianLayout", "w:specVanish", "w:oMath")


def open_doc(path):
    """Open the document and start revision ids above every id already used."""
    global _ids
    doc = Document(path)
    used = [int(v) for v in doc.element.body.xpath(".//@w:id") if v.lstrip("-").isdigit()]
    _ids = itertools.count(max(used, default=0) + 1)
    return doc


def rev(tag, **attrs):
    """A revision element (w:ins, w:del, w:moveFrom, w:moveTo, w:rPrChange, range starts)."""
    base = {qn("w:id"): str(next(_ids)), qn("w:author"): AUTHOR, qn("w:date"): DATE}
    return OxmlElement(tag, attrs={**base, **{qn(f"w:{k}"): v for k, v in attrs.items()}})


def paragraphs(doc):
    """All paragraphs in reading order, table cells included. Index them ONCE, up front."""
    return list(doc.element.body.iter(qn("w:p")))


def visible_runs(p):
    return p.xpath("./w:r | ./w:hyperlink/w:r | ./w:ins/w:r | ./w:moveTo/w:r")


def run_text(r):
    return "".join((c.text or "") if c.tag == W_T else "\t" if c.tag == W_TAB else "" for c in r)


def para_text(p):
    return "".join(run_text(r) for r in visible_runs(p))


def top(el, p):
    """The ancestor-or-self of el that is a direct child of paragraph p."""
    while el.getparent() is not p:
        el = el.getparent()
    return el


def split_run(r, k):
    """Split run r at character k; the right half is inserted after r and returned."""
    right = copy.deepcopy(r)
    pos = 0
    for a, b in zip(list(r), list(right)):
        if a.tag == W_RPR:
            continue
        n = len(a.text or "") if a.tag == W_T else 1 if a.tag == W_TAB else 0
        if pos + n <= k:
            right.remove(b)
        elif pos >= k:
            r.remove(a)
        else:
            a.text, b.text = a.text[: k - pos], a.text[k - pos :]
            for t in (a, b):
                t.set(qn("xml:space"), "preserve")
        pos += n
    for change in right.iter(qn("w:rPrChange")):  # revision ids must stay unique
        change.set(qn("w:id"), str(next(_ids)))
    r.addnext(right)
    return right


def isolate(p, find, occurrence=1):
    """Split runs so the exact text `find` is covered by whole runs; return those runs."""
    text, start = para_text(p), -1
    for _ in range(occurrence):
        start = text.find(find, start + 1)
        if start < 0:
            raise ValueError(f"{find!r} not found in {text!r}")
    end = start + len(find)
    for cut in (start, end):
        pos = 0
        for r in visible_runs(p):
            n = len(run_text(r))
            if pos < cut < pos + n:
                split_run(r, cut - pos)
                break
            pos += n
    out, pos = [], 0
    for r in visible_runs(p):
        n = len(run_text(r))
        if n and start <= pos and pos + n <= end:
            out.append(r)
        pos += n
    return out


def wrap(runs, tag):
    """Wrap runs in revision elements, one per group of adjacent siblings."""
    out = []
    for r in runs:
        if out and r.getprevious() is out[-1]:
            out[-1].append(r)
        else:
            el = rev(tag)
            r.addprevious(el)
            el.append(r)
            out.append(el)
    return out


def new_run(text, like=None):
    r = OxmlElement("w:r")
    if like is not None and like.find(W_RPR) is not None:
        rpr = copy.deepcopy(like.find(W_RPR))
        for old in rpr.findall(qn("w:rPrChange")):
            rpr.remove(old)
        r.append(rpr)
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    r.append(t)
    return r


def colorize(runs, kind, existing=True):
    """Paint runs in the review colour of `kind`. For text that already existed the old
    formatting is kept in w:rPrChange (so Reject All restores it). No-op when not REVIEW."""
    if not REVIEW:
        return
    color, fill = PALETTE[kind]
    for r in runs:
        rpr = r.get_or_add_rPr()
        change = rpr.find(qn("w:rPrChange"))
        if change is not None:
            rpr.remove(change)  # keep the ORIGINAL formatting recorded by an earlier change
        elif existing:
            change = rev("w:rPrChange")
            change.append(copy.deepcopy(rpr))
        run = Run(r, None)
        if color:
            run.font.color.rgb = RGBColor.from_string(color)
        if kind == "flag":
            run.font.underline = WD_UNDERLINE.DOUBLE
        if fill:
            for old in rpr.findall(qn("w:shd")):
                rpr.remove(old)
            shd = OxmlElement("w:shd", attrs={qn("w:val"): "clear", qn("w:color"): "auto", qn("w:fill"): fill})
            rpr.insert_element_before(shd, *_AFTER_SHD)
        if change is not None:
            rpr.append(change)  # w:rPrChange must stay the LAST child


def mark_paragraph(p, tag):
    """Flag the paragraph mark itself (w:pPr/w:rPr) as inserted / deleted / moved."""
    ppr = p.get_or_add_pPr()
    rpr = ppr.find(W_RPR)
    if rpr is None:
        rpr = OxmlElement("w:rPr")
        after = ppr.find(qn("w:sectPr"))
        after = after if after is not None else ppr.find(qn("w:pPrChange"))
        after.addprevious(rpr) if after is not None else ppr.append(rpr)
    rpr.insert(0, rev(tag))


def copy_ppr(src, dst):
    ppr = src.find(W_PPR)
    if ppr is not None:
        ppr = copy.deepcopy(ppr)
        for x in ppr.xpath("./w:sectPr | ./w:pPrChange | ./w:rPr"):
            ppr.remove(x)
        dst.insert(0, ppr)


def mark_deleted(runs):
    """Deleted text lives in w:delText (and field codes in w:delInstrText)."""
    for r in runs:
        for t in r.findall(W_T):
            t.tag = qn("w:delText")
        for t in r.findall(qn("w:instrText")):
            t.tag = qn("w:delInstrText")


def track_delete(p, find, occurrence=1):
    runs = isolate(p, find, occurrence)
    colorize(runs, "removed")
    mark_deleted(runs)
    return wrap(runs, "w:del")


def track_replace(p, find, new, occurrence=1):
    """Old text red + struck, new wording blue. Returns (first del, ins) for comments."""
    like = isolate(p, find, occurrence)[0]
    template = copy.deepcopy(like)
    dels = track_delete(p, find, occurrence)
    ins = rev("w:ins")
    r = new_run(new, template)
    ins.append(r)
    dels[-1].addnext(ins)
    colorize([r], "rephrased", existing=False)
    return dels[0], ins


def track_insert(p, text, after=None, occurrence=1):
    """Insert (green) after the exact text `after`; after="" = paragraph start, None = end."""
    ins, runs = rev("w:ins"), visible_runs(p)
    if after is None:
        r = new_run(text, runs[-1] if runs else None)
        ins.append(r)
        p.append(ins)
    elif after == "":
        r = new_run(text, runs[0] if runs else None)
        ins.append(r)
        ppr = p.find(W_PPR)
        ppr.addnext(ins) if ppr is not None else p.insert(0, ins)
    else:
        anchor = isolate(p, after, occurrence)[-1]
        r = new_run(text, anchor)
        ins.append(r)
        top(anchor, p).addnext(ins)
    colorize([r], "added", existing=False)
    return ins


def track_format(p, find, bold=None, italic=None, underline=None, occurrence=1):
    """Tracked run-formatting change, shaded yellow."""
    runs = isolate(p, find, occurrence)
    for r in runs:
        rpr = r.get_or_add_rPr()
        old = rpr.find(qn("w:rPrChange"))
        if old is not None:
            rpr.remove(old)
        change = old if old is not None else rev("w:rPrChange")
        if old is None:
            change.append(copy.deepcopy(rpr))
        run = Run(r, None)
        for name, value in (("bold", bold), ("italic", italic), ("underline", underline)):
            if value is not None:
                setattr(run, name, value)
        rpr.append(change)
    colorize(runs, "styled")
    return runs


def track_paragraph_style(p, style_id):
    """Tracked paragraph-style change (e.g. to "Heading2", "ListBullet"), shaded yellow."""
    ppr = p.get_or_add_pPr()
    old = copy.deepcopy(ppr)
    for x in old.xpath("./w:rPr | ./w:sectPr | ./w:pPrChange"):
        old.remove(x)
    for x in ppr.findall(qn("w:pPrChange")):
        ppr.remove(x)
    ppr.style = style_id
    change = rev("w:pPrChange")
    change.append(old)
    ppr.append(change)  # w:pPrChange must be the LAST child of w:pPr
    colorize(visible_runs(p), "styled")
    return p


def insert_paragraph_after(anchor, text, style_id=None, like=None):
    """New paragraph (green) after `anchor`. Formatting is copied from `like` (default:
    the anchor) - pass a body paragraph when the anchor is a heading or the name line."""
    like = anchor if like is None else like
    p = OxmlElement("w:p")
    copy_ppr(like, p)
    if style_id:
        p.get_or_add_pPr().style = style_id
    runs = visible_runs(like)
    ins = rev("w:ins")
    r = new_run(text, runs[0] if runs else None)
    ins.append(r)
    p.append(ins)
    mark_paragraph(p, "w:ins")
    anchor.addnext(p)
    colorize([r], "added", existing=False)
    return p


def delete_paragraph(p):
    """Remove a whole paragraph (red, struck), including its paragraph mark."""
    runs = visible_runs(p)
    colorize(runs, "removed")
    mark_deleted(runs)
    wrap(runs, "w:del")
    mark_paragraph(p, "w:del")


def move_paragraph_after(p, anchor, name):
    """Tracked move (purple at both ends) of p to just after anchor. Do moves FIRST."""
    runs = visible_runs(p)
    dest = OxmlElement("w:p")
    copy_ppr(p, dest)
    start = rev("w:moveToRangeStart", name=name)
    moved = rev("w:moveTo")
    for r in runs:
        clone = copy.deepcopy(r)
        for change in clone.iter(qn("w:rPrChange")):  # new content: no history, no duplicate ids
            change.getparent().remove(change)
        moved.append(clone)
    dest.extend([start, moved, OxmlElement("w:moveToRangeEnd", attrs={qn("w:id"): start.get(qn("w:id"))})])
    mark_paragraph(dest, "w:moveTo")
    colorize(list(moved), "moved", existing=False)
    colorize(runs, "moved")
    from_start = rev("w:moveFromRangeStart", name=name)
    wrappers = wrap(runs, "w:moveFrom")
    top(wrappers[0], p).addprevious(from_start)
    top(wrappers[-1], p).addnext(
        OxmlElement("w:moveFromRangeEnd", attrs={qn("w:id"): from_start.get(qn("w:id"))})
    )
    mark_paragraph(p, "w:moveFrom")
    anchor.addnext(dest)
    return dest


def add_comment(doc, p, first, last, text):
    """Comment on the content from `first` to `last` (inside paragraph p). No-op when not REVIEW."""
    if not REVIEW:
        return
    cid = str(doc.comments.add_comment(text=text, author=AUTHOR, initials=INITIALS).comment_id)
    top(first, p).addprevious(OxmlElement("w:commentRangeStart", attrs={qn("w:id"): cid}))
    end = OxmlElement("w:commentRangeEnd", attrs={qn("w:id"): cid})
    top(last, p).addnext(end)
    ref = OxmlElement("w:r")
    ref.append(OxmlElement("w:commentReference", attrs={qn("w:id"): cid}))
    end.addnext(ref)


def flag_wrong(doc, p, find, alternative, reason, occurrence=1):
    """Suspected wrong data: dark red on pink, double underline + comment with the alternative."""
    runs = isolate(p, find, occurrence)
    colorize(runs, "flag")
    add_comment(doc, p, runs[0], runs[-1], f"Possible error: {reason}\nSuggested: {alternative}")
    return runs


def add_legend(doc, P):
    """The colour legend as the first comment, on the first paragraph with text."""
    first = next(p for p in P if visible_runs(p))
    runs = visible_runs(first)
    add_comment(doc, first, runs[0], runs[-1], LEGEND)


def enable_track_changes(doc):
    """Review mode on: later edits in Word are tracked too (schema order matters)."""
    settings = doc.settings.element
    if settings.find(qn("w:trackRevisions")) is not None:
        return
    later = {qn(f"w:{t}") for t in (
        "doNotTrackMoves doNotTrackFormatting documentProtection autoFormatOverride "
        "styleLockTheme styleLockQFSet defaultTabStop autoHyphenation consecutiveHyphenLimit "
        "hyphenationZone doNotHyphenateCaps evenAndOddHeaders bookFoldRevPrinting "
        "bookFoldPrinting bookFoldPrintingSheets drawingGridHorizontalSpacing "
        "drawingGridVerticalSpacing displayHorizontalDrawingGridEvery "
        "displayVerticalDrawingGridEvery doNotUseMarginsForDrawingGridOrigin "
        "drawingGridHorizontalOrigin drawingGridVerticalOrigin doNotShadeFormData "
        "noPunctuationKerning characterSpacingControl printTwoOnOne strictFirstAndLastChars "
        "noLineBreaksAfter noLineBreaksBefore savePreviewPicture doNotValidateAgainstSchema "
        "saveInvalidXml ignoreMixedContent alwaysShowPlaceholderText doNotDemarcateInvalidXml "
        "saveXmlDataOnly useXSLTWhenSaving saveThroughXslt showXMLTags alwaysMergeEmptyNamespace "
        "updateFields hdrShapeDefaults footnotePr endnotePr compat docVars rsids attachedSchema "
        "themeFontLang clrSchemeMapping doNotIncludeSubdocsInStats doNotAutoCompressPictures "
        "forceUpgrade captions readModeInkLockDown smartTagType schemaLibrary shapeDefaults "
        "doNotEmbedSmartTags decimalSymbol listSeparator").split()} | {
        "{http://schemas.openxmlformats.org/officeDocument/2006/math}mathPr"}
    el = OxmlElement("w:trackRevisions")
    nxt = next((c for c in settings if c.tag in later), None)
    nxt.addprevious(el) if nxt is not None else settings.append(el)


def accept_all(doc):
    """Apply every tracked change: the clean final copy (no revisions, no comment anchors)."""
    body, w = doc.element.body, lambda t: qn(f"w:{t}")
    gone = {w("del"), w("moveFrom")}
    for p in list(body.iter(w("p"))):
        mark = p.find(f"{W_PPR}/{W_RPR}")
        if mark is not None and any(c.tag in gone for c in mark) and not para_text(p):
            siblings = p.getparent().findall(w("p"))
            if p.getparent().tag != w("tc") or len(siblings) > 1:
                p.getparent().remove(p)
    for el in list(body.iter(w("del"), w("moveFrom"))):
        el.getparent().remove(el)  # deleted content, and leftover mark flags
    for el in list(body.iter(w("ins"), w("moveTo"))):
        parent = el.getparent()
        if parent.tag != W_RPR:
            for child in list(el):
                el.addprevious(child)
        parent.remove(el)
    for tag in ("rPrChange", "pPrChange", "moveFromRangeStart", "moveFromRangeEnd",
                "moveToRangeStart", "moveToRangeEnd", "commentRangeStart", "commentRangeEnd"):
        for el in list(body.iter(w(tag))):
            el.getparent().remove(el)
    for ref in list(body.iter(w("commentReference"))):
        ref.getparent().getparent().remove(ref.getparent())
    for el in doc.settings.element.findall(w("trackRevisions")):
        el.getparent().remove(el)


def to_redline(doc):
    """Turn the tracked review into a visual diff made only of direct formatting: removed
    and moved-away text stays visible, struck through, in its review colour. Looks the
    same in every viewer (Word markup colours override text colour; this has no markup)."""
    body, w = doc.element.body, lambda t: qn(f"w:{t}")
    for el in list(body.iter(w("del"), w("moveFrom"), w("ins"), w("moveTo"))):
        parent = el.getparent()
        if parent.tag == W_RPR:  # paragraph-mark flag
            parent.remove(el)
            continue
        struck = el.tag in (w("del"), w("moveFrom"))
        for r in el.findall(w("r")):
            for t in r.findall(qn("w:delText")):
                t.tag = W_T
            for t in r.findall(qn("w:delInstrText")):
                t.tag = qn("w:instrText")
            if struck:
                change = r.find(f"{W_RPR}/{qn('w:rPrChange')}")
                if change is not None:
                    change.getparent().remove(change)
                Run(r, None).font.strike = True
        for child in list(el):
            el.addprevious(child)
        parent.remove(el)
    for tag in ("rPrChange", "pPrChange", "moveFromRangeStart", "moveFromRangeEnd",
                "moveToRangeStart", "moveToRangeEnd"):
        for el in list(body.iter(w(tag))):
            el.getparent().remove(el)
    for el in doc.settings.element.findall(w("trackRevisions")):
        el.getparent().remove(el)


def render(path, view="accepted"):
    """Paragraph texts with all changes accepted, or all rejected ("original")."""
    hide = {qn("w:del"), qn("w:moveFrom")} if view == "accepted" else {qn("w:ins"), qn("w:moveTo")}
    lines = []
    for p in Document(path).element.body.iter(qn("w:p")):
        mark = p.find(W_PPR + "/" + W_RPR)
        gone = mark is not None and any(c.tag in hide for c in mark)
        text = "".join(
            (t.text or "") if t.tag != W_TAB else "\t"
            for t in p.iter(W_T, qn("w:delText"), W_TAB)
            if not any(a.tag in hide for a in t.iterancestors())
        )
        if not (gone and not text):
            lines.append(text)
    return lines
```

## Example script body

`build(mode, out)` makes the same edits in every mode: "review" = colours +
comments + tracked; "redline" = the same, then `to_redline`; "final" = no
colours/comments, then `accept_all`. Indexes
come from `paragraphs(doc)` taken once, so earlier edits do not shift later
ones. Wrap each change so one failure does not stop the rest.

```python
# --- example ---
from pathlib import Path

SRC = "/input/cv.docx"
JOB_DIR, STEM, SLUG, ROUND = "/output/resume/1-acme-backend", "cv", "acme-backend", 1
REVIEW_OUT = f"{JOB_DIR}/v{ROUND}/{STEM}_{SLUG}_review.docx"
REDLINE_OUT = f"{JOB_DIR}/v{ROUND}/{STEM}_{SLUG}_redline.docx"
FINAL_OUT = f"{JOB_DIR}/v{ROUND}/{STEM}_{SLUG}_final.docx"


def build(mode, out):
    """mode: "review" (tracked + colours + comments), "redline" (colour diff, no
    tracking), "final" (everything applied, clean)."""
    global REVIEW
    REVIEW = mode != "final"
    review = REVIEW
    doc = open_doc(SRC)
    P = paragraphs(doc)
    results = []
    add_legend(doc, P)  # first comment; no-op in final mode

    def change(label, fn):
        try:
            fn()
            results.append(f"OK   {label}")
        except Exception as exc:  # report and carry on
            results.append(f"FAIL {label}: {type(exc).__name__}: {exc}")

    def reorder_skills():  # moves first
        move_paragraph_after(P[4], P[2], "move1")

    def rephrase_title():
        d, i = track_replace(P[1], "Python developer", "Senior Python engineer")
        add_comment(doc, P[1], d, i, "Posting title: Senior Python Engineer")

    def add_fastapi():
        i = track_insert(P[3], ", FastAPI", after="Django")
        add_comment(doc, P[3], i, i, "FastAPI is a required skill; shown in your Acme work")

    def summary():
        new = insert_paragraph_after(P[0], "Backend engineer: 5 years building Python REST APIs.", like=P[1])
        add_comment(doc, new, new[-1], new[-1], "Added summary: ATS and recruiters read it first")

    def ask_kafka():
        runs = isolate(P[3], "SQL")
        add_comment(doc, P[3], runs[0], runs[-1], "The job requires Kafka - have you used it?")

    def heading_style():
        track_paragraph_style(P[6], "Heading2")

    def wrong_dates():
        flag_wrong(doc, P[6], "2020–2025", "2020–Present", "role end date is in the future")

    change("move skills bullet", reorder_skills)
    change("title", rephrase_title)
    change("fastapi", add_fastapi)
    change("summary", summary)
    change("kafka question", ask_kafka)
    change("company line as heading", heading_style)
    change("bold company", lambda: track_format(P[6], "Acme Corp", bold=True))
    change("flag dates", wrong_dates)
    change("drop tab clause", lambda: track_delete(P[7], "\tLed a team of 3."))
    change("new bullet", lambda: insert_paragraph_after(P[7], "Led a team of 3 engineers; cut latency 40%."))
    change("drop references line", lambda: delete_paragraph(P[8]))

    if mode == "review":
        enable_track_changes(doc)
    elif mode == "redline":
        to_redline(doc)
    else:
        accept_all(doc)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    doc.save(out)
    return results


results = build("review", REVIEW_OUT)
build("redline", REDLINE_OUT)
build("final", FINAL_OUT)
print("\n".join(results))
original, accepted, final = render(SRC, "original"), render(REVIEW_OUT, "accepted"), render(FINAL_OUT, "original")
print("CHECK reject-all == original:", render(REVIEW_OUT, "original") == original)
print("CHECK accept-all == final:", accepted == final)
print("CHECK final is clean:", not Document(FINAL_OUT).element.body.xpath(".//w:ins | .//w:del | .//w:moveTo | .//w:commentReference"))
print("CHECK redline has no tracking:", not Document(REDLINE_OUT).element.body.xpath(".//w:ins | .//w:del | .//w:moveFrom | .//w:moveTo"))
print("FINAL:", *final, sep="\n  ")
```

## Rules

- Copy `find` / `after` text **exactly** from your dump (dashes, spacing, tabs).
- Moves first, then edit the moved copy (the paragraph `move_paragraph_after`
  returns) so a move stays one clean moveFrom/moveTo pair.
- Every change gets a comment with its reason (job requirement, ATS rule,
  feedback item). One comment per change, short.
- Truthfulness: rephrase, reorder, quantify only with facts already in the
  resume. Missing facts are asked for in comments, never invented.
- Keep the user's fonts and layout; styles only change towards standard ATS
  headings and bullets.
- Two jobs = two folders; every round starts again from the same input copy.
- Your script, `v<k>/` rounds and `change_log.md` are working files: the
  orchestrator removes them at the end. What stays are the three .docx
  deliverables it copies to the job folder - so put every question for the user
  in a comment, not only in the change log.
