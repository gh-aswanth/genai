---
name: web-job-search
description: How to find and extract job postings with the Playwright MCP browser tools - inspect the live page, write a JavaScript extractor that fits it, paginate, open detail pages, and save normalised records to /output/jobs/jobs.json. Includes the recovery ladder for empty results.
---

# Web job search with Playwright MCP

Method, always: LOOK at the real page, WRITE an extractor that fits what you saw,
RUN it with `browser_evaluate`, SAVE the result. Never assume selectors.

## Sources

Use the sites or URLs the task names. If none are named, search these and note
which ones you used in `/output/jobs/search_notes.md`:

- `https://infopark.in/companies-job` (Kerala IT parks; tables with detail pages)
- `https://technopark.in/job-search`
- `https://www.linkedin.com/jobs/search/?keywords=<q>&location=<loc>` (public, no login)
- `https://www.naukri.com/<q>-jobs-in-<loc>`

Skip a site that demands login or shows a CAPTCHA - note it and move on.

## Navigation - every time, in this order

1. `browser_navigate({"url": "..."})`
2. `browser_wait_for({"time": 3})` - pages are not ready on arrival
3. then inspect or extract

Never pass invented arguments to a tool. Leave a second or two between page loads.

Big results: pass `filename` to `browser_evaluate` (a plain name like
`listing-p2.json`) - it is saved in the browser's captures folder and readable
in the sandbox at `/output/.browser/captures/<name>`; merge it into
`/output/jobs/` with a script. Never pass directories or absolute paths.

## Step 1 - look at the page

```js
() => { const tables=[...document.querySelectorAll('table')].map((t,i)=>({i,headers:[...t.querySelectorAll('th')].map(h=>h.innerText.trim()),rows:t.querySelectorAll('tbody tr').length,sample:[...(t.querySelector('tbody tr')?.querySelectorAll('td')||[])].map(d=>d.innerText.trim().slice(0,50)),rowLinks:[...(t.querySelector('tbody tr')?.querySelectorAll('a[href]')||[])].map(a=>a.getAttribute('href'))})); const s={}; for(const a of document.querySelectorAll('a[href]')){const k=a.getAttribute('href').replace(/[0-9]+/g,'#');(s[k]=s[k]||{n:0,eg:a.getAttribute('href')}).n++;} const links=Object.entries(s).map(([k,v])=>({shape:k,...v})).sort((a,b)=>b.n-a.n).slice(0,20); return {len:document.body.innerText.length,title:document.title,tables,links}; }
```

Decide and write to `/output/jobs/search_notes.md`: which table (or repeated card
element) holds the jobs, the detail-link shape, and the pagination shape. With no
tables, find elements that repeat many times (tag + class) and use those.

## Step 2 - extract listing rows

Per listing page: navigate, wait, evaluate an extractor shaped to what you found,
for example:

```js
() => { const t=document.querySelectorAll('table')[TABLE_INDEX]; const heads=[...t.querySelectorAll('th')].map(h=>h.innerText.trim()); return [...t.querySelectorAll('tbody tr')].map(tr=>{ const c=[...tr.querySelectorAll('td')].map(td=>td.innerText.trim()); const a=[...tr.querySelectorAll('a[href]')].find(a=>a.href.includes('DETAIL_PART')); const o={}; c.forEach((v,n)=>o[heads[n]||('col'+n)]=v); o.url=a?a.href:null; return o; }); }
```

Append each page's rows to `/output/jobs/listings.json` as you go and report the
count per page. Never hold hundreds of rows only in your messages. Stop at the
number of jobs the task asks for (default 25) - filter by the role keywords first.

## Step 3 - detail pages

For each kept listing: navigate to its url, wait, then

```js
() => { const m=document.querySelector('main')||document.body; const t=m.innerText; return {text:t.trim().slice(0,6000), emails:[...new Set((t.match(/[\w.+-]+@[\w-]+\.[\w.]+/g)||[]))]}; }
```

Work in batches of about 10. A page that fails gets an `"error"` field; carry on.

## Step 4 - normalise and save

Write `/output/jobs/jobs.json` as a JSON array; one object per job:

```json
{
  "id": "infopark-25668",
  "title": "Senior Python Developer",
  "company": "Acme Technologies",
  "location": "Kochi, Kerala",
  "employment_type": "Full-time",
  "experience": "4-6 years",
  "posted": "2026-09-20",
  "deadline": "2026-10-05",
  "url": "https://...",
  "source": "infopark.in",
  "contact_emails": ["hr@acme.example"],
  "description": "full text of the posting (<= 6000 chars)",
  "required_skills": ["Python", "Django", "PostgreSQL"],
  "nice_to_have": ["AWS"]
}
```

Unknown fields are `null` - never invent data. `required_skills` / `nice_to_have`
come from the posting text only. Use `execute` (Python is available in the
sandbox, no network) to merge, de-duplicate by url and count records.

## Step 5 - verify

`execute` a count of `jobs.json` records and compare with the listings you kept.
Report gaps and which sites you used.

## When a result comes back empty - work the ladder

1. Did you wait? `browser_wait_for({"time": 3})`, evaluate again.
2. Any text? `() => document.body.innerText.length` - over 200 means the page is
   loaded and your selector is wrong: go back to Step 1.
3. In a frame? `() => [...document.querySelectorAll('iframe')].map(f=>f.src)` -
   navigate to the frame URL.
4. Redirected? `() => location.href`
5. Still nothing: `browser_take_screenshot` and `browser_snapshot` (no arguments).

Only after all five may you report a block, saying which tool returned what.
