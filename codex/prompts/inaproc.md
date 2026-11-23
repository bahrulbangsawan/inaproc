---
description: Search the INAPROC e-catalogue, compare products, check sellers, manage my own penyedia products, export JSON/CSV/XLSX/YAML
argument-hint: [what to find, e.g. cheapest kertas HVS A4 80 gram]
---

Use the `inaproc` skill (installed at ~/.codex/skills/inaproc) for this request: $ARGUMENTS

Read ~/.codex/skills/inaproc/SKILL.md first and follow it. In particular, ask me which output format I want (chat only, JSON, CSV, XLSX, YAML) and where to save it (this project → ./output/, or another path) before running the script.

For a RAB comparison with my company's link, also ask whether my company should win (comparisons more expensive) or whether to find the cheapest, and follow references/rab-comparison.md.

For my own penyedia products (list, add, update, bulk-update, delete), use the `penyedia` commands in SKILL.md: every write is a dry run first; show me the plan and wait for my explicit yes before `penyedia apply`.
