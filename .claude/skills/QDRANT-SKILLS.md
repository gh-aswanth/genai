# Vendored Qdrant skills

The `qdrant-*` folders here are copied from https://github.com/qdrant/skills
(commit `56f8fef`, 2026-09-28), licensed Apache-2.0 (see `QDRANT-LICENSE`).

Claude Code auto-discovers them for anyone who clones this repo.

To update:

```bash
git clone --depth 1 https://github.com/qdrant/skills /tmp/qdrant-skills
rm -rf .claude/skills/qdrant-*
for d in /tmp/qdrant-skills/skills/qdrant-*; do cp -R "$d" .claude/skills/; done
```
