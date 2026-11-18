---
name: two-desk-paper-experiment
description: Two paper trading desks run side by side since 2026-11-10; compare them around 2026-11-17 before changing any rules
metadata:
  type: project
---

Since 2026-11-10 12:36 (Swedish time) two paper desks run in parallel on $200 each, same agents and
filter, different sell rules: the main desk (stop -8 %, trail 8 % after +15 %, started 2026-11-08) and
the "daytrade" desk (`--profile daytrade`: stop -3 %, take profit +4.5 %, max hold 8 h, 120 min re-entry
cooldown). `python -m desk compare` puts them side by side, counted from the daytrade desk's start.

**Why:** the user wants day trades and was unsure whether to switch strategy or raise the 10-position
limit. The agreed plan was to leave the main desk untouched for a week and let a second desk test the
daytrade rules, then decide from the comparison (around 2026-11-17).

**How to apply:** do not change the main desk's rules or reset either book before the comparison unless
the user asks. When reading the comparison, point out the exposure difference (main started nearly
fully invested, daytrade in cash). Both desks are paper only: never start live mode or place real
orders, never read out or type API keys (the user fills `.env`), and keep strategy talk as system
design and paper-test reasoning, not investment advice. The desks run as background loops of the
session; if they have stopped, `run_desk.bat` and `run_daytrade.bat` start them again.

Related: [[user-replies-in-swedish]]
