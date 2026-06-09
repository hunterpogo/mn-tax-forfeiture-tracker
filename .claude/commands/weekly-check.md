# /weekly-check

Run the weekly Minnesota tax-forfeited land sale check.

The Python scraper runs on GitHub Actions (not in this sandbox — direct fetching of county government sites is blocked here). This command reads the latest committed report and creates the Gmail draft.

---

## Step 1 — Load tools

Load these via ToolSearch before starting:
- `mcp__github__get_file_contents`
- `mcp__github__actions_run_trigger`
- `mcp__github__actions_list`
- `mcp__Gmail__create_draft`

---

## Step 2 — Check for today's report

Use `mcp__github__get_file_contents` to read:

```
owner: hunterpogo
repo:  mn-tax-forfeiture-tracker
path:  reports/weekly_report_YYYY-MM-DD.md   ← today's date
ref:   main
```

**If the file exists → skip to Step 5.**

**If it doesn't exist → continue to Step 3.**

---

## Step 3 — Trigger the GitHub Actions workflow

Use `mcp__github__actions_run_trigger` to fire a manual run:

```
owner:         hunterpogo
repo:          mn-tax-forfeiture-tracker
workflow_file: weekly-check.yml
branch:        main
```

Tell the user: "Workflow triggered. The scraper checks ~87 county URLs with a 2-second delay between each, so this typically takes 5–15 minutes."

---

## Step 4 — Wait for the workflow to complete

Poll `mcp__github__actions_list` for the most recent run of `weekly-check.yml`. Check roughly every 2 minutes (make a tool call, report "still running…", repeat) until:

- `status = completed` AND `conclusion = success` → continue to Step 5
- `status = completed` AND `conclusion = failure` → report the failure and stop
- 30 minutes elapsed without completion → report a timeout and stop

---

## Step 5 — Read the report

Use `mcp__github__get_file_contents` to read:

```
owner: hunterpogo
repo:  mn-tax-forfeiture-tracker
path:  reports/weekly_report_YYYY-MM-DD.md   ← today's date
ref:   main
```

The content is base64-encoded — decode it to get the markdown text.

If today's file still doesn't exist after a successful workflow run, read the most recently dated file under `reports/`.

---

## Step 6 — Output the report

Print the full decoded markdown report in the conversation.

---

## Step 7 — Create the Gmail draft

Use `mcp__Gmail__create_draft`:

```
to:      info@wimnre.com
subject: MN Tax-Forfeited Land Sales — Weekly Report YYYY-MM-DD
body:    <the full report text>
```

Report the draft ID when done.
