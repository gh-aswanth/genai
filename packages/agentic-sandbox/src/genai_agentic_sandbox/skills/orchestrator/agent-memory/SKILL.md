---
name: agent-memory
description: How the orchestrator uses and maintains long-term memory in /memories - user_profile.md (confirmed facts and preferences, answers to resume questions) and agent_notes.md (lessons from earlier runs) - what to save from the user and from subagents' "Memory notes", what never to save, and when. Use at the start and end of every run.
---

# Long-term memory

Two files in `/memories/` survive between runs (they live on the user's machine,
outside the output folder, and cleanup never touches them). They are loaded
into your prompt; subagents that need them load them read-only.

| File | Keep in it | Never |
|---|---|---|
| `/memories/user_profile.md` | job preferences (roles, seniority, locations, remote, companies to avoid), facts the user confirmed for the resume ("Kafka: used at Acme 2023-24"), links (LinkedIn, GitHub), style preferences | guesses, facts from job postings, the resume's full text, anything unconfirmed |
| `/memories/agent_notes.md` | per job site: works / blocks / needs login + the extractor shape that worked; resume changes that moved ATS scores; mistakes to avoid | job listings, scores of one run, temporary state |

Never store passwords, API keys, tokens or other credentials - even if the user
pastes them.

## At the start (one todo)

Read both files (already in your prompt). Use them: default the search to the
user's stated preferences when the request is vague; tell the resume-builder in
its brief that confirmed facts are in the profile.

## During the run

When the user answers a question or states a preference ("I did use Kafka",
"only remote roles", "my LinkedIn is ..."), save it to `user_profile.md` in the
same turn with `edit_file`, under the right heading - one line per fact, dated
when useful.

## At the end (one todo, BEFORE `output-cleanup`)

1. Collect every subagent's "Memory notes" from its final answer.
2. Keep only what is durable and verified: a site that blocked twice, an
   extractor that worked, a resume change that raised the ATS score. Drop
   one-off noise.
3. `edit_file` into `agent_notes.md`: update the existing line for that site or
   pattern instead of appending duplicates; delete lines that proved wrong.
4. Keep each file under ~150 lines - merge and prune when it grows.

The user can edit both files by hand (for example answering last run's resume
questions before the next run). Their edits win over yours.
