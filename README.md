# curious-download

Download and decrypt Curious (MindLogger) answer data from the command line, filtered by
participant, date range, and activity. The output is a `responses.csv` in the same format as
the admin panel's **Export data** (renamed-column version), so the existing
`mindlogger-data-export` post-processing tool and transformation scripts can read it.

The same command also downloads [data dictionaries](#data-dictionaries): one CSV per activity
describing its items across every version of the applet. It can [convert REDCap data
dictionaries](#converting-a-redcap-data-dictionary) into that format, and [upload a data
dictionary](#uploading-a-data-dictionary-into-an-applet) into an existing applet.

Prefer point-and-click? [`curious-app`](#the-app-curious-app) runs the same downloads
(answers, data dictionaries, and participant counts) from a page in your browser.

## Install

Requires Python 3.11+.

```bash
pip install -e /path/to/curious-data-download
```

or with uv:

```bash
uv tool install --editable /path/to/curious-data-download
```

Install with `-e` / `--editable` so the command always runs the current code. A plain install
copies the code once and keeps running that copy after the folder is updated.

## Use

### Interactive

Run it with no options and answer the questions:

```bash
curious-download
```

It first asks what you want to do: download answers, download data dictionaries, convert a REDCap
data dictionary, or upload a data dictionary into an applet. For answers, it then asks for the API
address, your email and password (plus a 6-digit code if your account uses MFA), then the applet
and its applet password. Then you choose participants, a date range, and
activities. At the end it prints the equivalent command, so you can repeat the same download
without the questions.

### Scripted

Every question has an option. Anything you leave out means "all" when `--no-input` is set:

```bash
export CURIOUS_PASSWORD=...            # account password
export CURIOUS_APPLET_PASSWORD=...     # applet (encryption) password
curious-download --no-input \
  --server https://<curious-api-address> --email you@example.org \
  --applet "EMA Study" \
  --participant P001 --participant P002 \
  --from 2026-09-01 --to 2026-09-30 \
  --activity "EMA Morning" \
  --output ./exports
```

| Option | Meaning |
|---|---|
| `--server` / `CURIOUS_API_URL` | Address of the Curious **backend API**, not the admin website |
| `--email` / `CURIOUS_EMAIL` | Your login |
| `--applet` | Applet ID or exact name |
| `--workspace` | Workspace name or owner ID, if you have several and want to narrow the applet search |
| `--participant`, `-p` | Participant secret ID (repeatable). Matches the participant the answers are **about** (target subject), as in the admin panel's per-participant export |
| `--from`, `--to` | Dates (`YYYY-MM-DD`) or datetimes in **your local time**; converted to UTC and compared with the time the server received each answer |
| `--activity`, `-a` / `--flow` | Activity or flow name or ID (repeatable) |
| `--media` | Also download the media files of the selected answers (photos, video, audio, drawings, Unity task files) |
| `--journey` | Also write `activity_user_journey.csv`, the participant journey: every next, back, undo, skip, answer, and done action with its time |
| `--mfa-code` | Two-factor code for scripted runs |
| `--page-size` | Answers per request (default 500, max 10000) |
| `--save-decrypted` | Also write `decrypted_answers.jsonl` with the raw decrypted answers and events |
| `--subscale-null-when-skipped` | Score skipped items as missing instead of 0 (mirrors the admin feature flag `enableSubscaleNullWhenSkipped`) |

## Output

Each run writes a new folder `<output>/<applet>_<timestamp>/` containing:

- **`responses.csv`**: one row per answered item, in the admin's column order and cell format
  (`value: 1`, `date: 5/9/2026`, media file names, and so on). It is UTF-8 with a BOM and LF line
  endings, with the admin's formula-injection protection applied. Subscale and total scores are
  added to the first row of each submission (`activity_score`, `subscale_name_<name>`, …).
- **`export_info.json`**: the filters used (with exact UTC bounds), counts, the tool version, and
  any submissions that could not be decrypted. It contains no passwords. Its `command` field is
  the full `curious-download` command that repeats this export, the same one the wizard prints at
  the end. It's recorded for wizard, scripted, and app runs alike. To rerun it unattended, add
  `--no-input` and set `CURIOUS_PASSWORD` and `CURIOUS_APPLET_PASSWORD`.

With `--media`, the folder also contains:

- **`media/`**: photos, video, audio, and drawings (SVG). Each file has the admin's name,
  `<target secret ID>-<submission ID>-<item name>.<extension>`, which is also what the
  `item_response` cell shows. Characters that aren't allowed in file names (such as `/` in a
  secret ID) become `_`.
- **`unity/<submission ID>/`**: Unity task files.
- **`media_files.csv`**: every media file with its submission, participant, item, saved path,
  and status. The status is `downloaded`; `saved_from_answer` (a drawing that was never uploaded,
  saved from the SVG stored in the answer); or `failed`, with the reason, for example a file the
  phone never uploaded or one the server refused to share. The admin panel skips such files
  silently; here they are listed. The file is written even when there are no media files.

With `--journey` (the app's "participant journey" checkbox), the folder also contains:

- **`activity_user_journey.csv`**: the admin panel's user-journey export, with the same 41 columns
  and cell format. There is one row per action the participant took in the app. The time (ms since
  1970, from the phone's clock) goes in the column for that action: `press_next_time`,
  `press_back_time`, `press_undo_time`, `press_skip_time`, `press_popup_skip_time`,
  `press_popup_keep_time`, `press_done_time`, or `response_option_selection_time` for an answer.
  Answer rows show the response chosen at that moment in `item_response`, so changed answers show
  up as several rows. A first action taken before the first item (a splash screen) gets an
  `item_name` of `Splash Screen`. The same participant, date, and activity filters apply.

The actions are stored encrypted separately from the answers. If they can't be decrypted for a
submission, its answers are still exported and `export_info.json` lists it under
`journey.actions_decryption_failures`; the admin panel leaves such submissions out silently.

Rows are ordered newest first, like the admin export. Unlike the admin, everything goes into
one file instead of one file per 250 answers, and media go into folders instead of zips.

## How it works

1. Logs in (`POST /auth/login`, then MFA if required) and refreshes the 30-minute access token
   automatically.
2. Downloads encrypted answers from `GET /answers/applet/{id}/data`. Participant and date filters
   are applied on the server.
3. Derives the applet's private key from the applet password and checks it against the applet's
   stored public key, so a wrong password fails right away.
4. Decrypts each answer (AES-256-CBC with a Diffie–Hellman key, as the mobile app encrypts it).
   Activity filtering happens here, because the export endpoint has no activity filter.
5. Formats rows with a line-by-line port of the admin panel's export code.
6. With `--media`, asks the server for temporary download links (`POST /file/{id}/presign`, valid
   for one hour, requested in batches of 50 just before downloading) and downloads the files four
   at a time. Your API login is never sent to the storage service; the links carry their own
   permission.

**Key derivation note.** The admin panel builds the applet key with a JavaScript string
conversion that depends on how the browser's `Buffer` polyfill decodes invalid UTF-8. Native
Node and Python decode it differently. The tool tries both and keeps the one that matches the
applet's public key. `export_info.json` records which one matched (`key_variant`).

## The app (`curious-app`)

The same three downloads as a local web app, for anyone who prefers forms over a terminal:

```bash
curious-app
```

It starts a small server on your own computer and opens the app in your browser. Log in once,
then pick a task:

- **Download answers** — choose a workspace, an applet (unlocked with its applet password),
  participants, a date range, activities, and optionally media files and the participant journey.
- **Data dictionary** — choose a workspace, all or some of its applets, and all saved versions or only the current one.
- **Participant counts** — choose a workspace and a date range; the table is shown in the page
  and saved as a CSV.
- **Dashboard** — who has done what, against the schedule, for a study spanning one or more
  applets. See [Dashboard](#dashboard).

Runs appear at the bottom with a progress bar and can be cancelled. Each finished run shows
what was written, a **Show in Finder** button, and the equivalent terminal command, so a
download set up by clicking can be repeated or scheduled as a script later. Files are written
to a folder you choose (`~/curious_exports` by default), in exactly the same formats as the
CLI tools.

Notes:

- Everything runs locally: the page talks only to the server on your computer, which talks
  only to the Curious API you log in to. Passwords stay in memory and are never written to disk.
- The page only works with the tokened link that `curious-app` prints (and opens) — another
  website or user on the computer cannot reach the API behind it.
- Keep the terminal window open while you use the app; Ctrl+C stops it.
- `--port` chooses the port (default 8456; the next free one is used if taken) and
  `--no-browser` skips opening the browser.

## Dashboard

The **Dashboard** tab of `curious-app` shows each participant's progress against the study
schedule: one row per participant, cells coloured by completed, missed, open, and upcoming. It
reads only schedules and submission times (`GET /applets/{id}/events`, the schedule history, the
participant list, and the metadata of `GET /answers/applet/{id}/data`). No answer is decrypted and
no applet password is needed, but reading schedules needs an owner, manager, or coordinator role on
the applet.

A **study** is a saved set-up: a name, the applets it uses (several at once is fine), the kind of
dashboard, and a few rules. Studies are stored in `~/.config/curious-download/dashboards/`, never
with any password, and are tied to the server you saved them on. Click **Load data** to read
everything from Curious; the data stays in memory until you log out, and every view is computed
from it on the spot.

### Keeping data between runs

After a load, the study's data (participants, activities, schedules, and completion times; never
answer content or passwords) is saved in `~/.config/curious-download/dashboards/cache/`, so the
next time you start the app and log in with the same account the dashboard is there at once.
**Refresh** then fetches only what changed since: completions received after the last load, new
participants, the default schedule, and the individual schedules whose history shows a change.
**Reload all** reads everything again; use it after a schedule change the history could not tell
you about, or if something looks stale. Changing a study's applets, aliases, or "data from" day
discards its saved data, and deleting the study deletes the file.

### The four kinds of dashboard

| Kind | For studies where… | Columns |
|---|---|---|
| **Fixed schedule** | everyone follows the same calendar: same start and end dates, activities repeating daily or weekly | days (or weeks) × activities; a cell shows done / expected |
| **Same schedule, own start date** | the protocol is the same but each participant starts on their own day | study day 1, 2, … × activities, so everyone lines up |
| **Cross-sectional** | each activity is done once | one per activity or flow; a cell is done, partly done, or not done |
| **Individual schedules** | every participant has their own calendar | days, with all of a participant's activities folded into the cell; click a participant for their own timeline |

For *own start date*, day 1 can be the participant's first completion (of any activity or of one
you choose), the start of their individual schedule, the day they were added to the applet, or a
date you type in. When the chosen source is missing for someone, the dashboard falls back to the
next one and says so in the row.

### How a cell is worked out

1. **Expected occurrences.** Each schedule event is expanded into the days it offered its
   activity, with the same rules as the mobile app: `ALWAYS` (no expectation; completions are
   counted), `ONCE`, `DAILY`, `WEEKLY` (the weekday of the selected date), `WEEKDAYS`, `MONTHLY`
   (clamped to short months). Each occurrence has the event's time window in the participant's
   local time; a window ending before it starts runs into the next day, and "access before
   schedule" opens it at midnight. An individual event replaces the default events for that one
   activity or flow only, as in the backend. Where the schedule history is readable (owner or
   manager), each version of an event only counts for the days it was in force, so a change made
   mid-study is not applied to the past; otherwise the current schedule is assumed throughout and
   the dashboard says so. Nothing is expected from before the day a participant was added to the
   applet (switch off in *Rules and matching*).
2. **Matching.** The phone records which event it was answering and the time it was scheduled
   for, which pins the exact occurrence even when the answer arrived late or after midnight.
   Older answers without that are matched to an occurrence of the same activity whose window
   contains the completion, or else the nearest one that day. A second completion in the same
   window, or one with no occurrence at all (always-available activities), is counted as
   *done, not scheduled*. Times use the offset the phone reported with each answer.
3. **Status.** *Completed*; *completed outside the time window* (counted as done unless you switch
   that off, and marked `*`); *partly done* for a flow with activities missing; *missed* once the
   window has closed; *open now*; *upcoming*. Adherence is done ÷ occurrences whose window has
   closed. Curious keeps unfinished activities on the phone only, so a half-finished questionnaire
   is invisible to the server; "partly done" therefore exists for flows alone.

Loading reads participants, the schedule history, and completions in pages (1,000 completions per
request; the admin panel uses 250). If the server refuses a page with an error, the page size is
halved and the load goes on; rows the server cannot return even ten at a time are skipped with a
warning, and a request that fails for another reason is named in the error message.

**Large studies.** With thousands of participants, choose who to load under **Participants to
load** in the study editor: everyone, only participants who have submitted data (found from the
submission metadata before any answer is read), or a list you pick after **Fetch the participant
list**, which shows each person's submission count. Completions, individual schedules, and the
schedule history are then read only for those people, a few participants per request; a request
that a proxy or firewall in front of the server refuses (an HTML 403 page) is split and retried. **Read the schedule history** can be switched
off in *Rules and matching* when it is too slow; the current schedule is then assumed throughout.

Participants are matched across applets by secret ID; the *aliases* rule maps IDs that belong to
the same person. Reviewer assessments, hidden and reviewer-only activities, and activities that
only exist inside a flow are left out. Participants removed from an applet are hidden unless you
ask for them; their data is still on the server.

### Choosing what to show

Above the grid, **Participants** opens a picker: a table of everyone loaded with nickname, tag,
applets, submission count, adherence, and last completion, which you can search, filter by data,
tag, or applet, sort, and tick or untick in bulk. **Activities** offers *All* or *Choose…* with a
tickable list and buttons to tick or untick everything, remove all flows, or remove all activities, **only participants with data** hides everyone who has not completed anything, and
**hide empty columns** (on by default) drops every column where nothing was scheduled or done for
anyone, so a study that mixes a daily schedule with one-off activities shows each day's own
activities only. The choices are saved on the study, so they are still in place the next time you
open it. The study editor has the same **Activities to show** list, and the same picker for
**Participants to load**, so an applet can be narrowed before the first load.

### Exports

**Export dashboard** writes the view you are looking at into a folder,
`<output>/dashboard_<study>_<view>_<timestamp>/`, and lists it under Runs with a Show in Finder
button:

| File | Contents |
|---|---|
| `export_info.json` | The study set-up (applets, type, rules), the view settings (range, columns, start rule, which activities), when the data was loaded and from where, counts, the status legend, the notes shown in the app, and what each file holds. No passwords. |
| `dashboard_cells.csv` | The grid as data: one row per participant and column, with the counts of expected occurrences by outcome (`expected`, `done`, `late`, `partly_done`, `missed`, `open_or_upcoming`, `unscheduled`), the `status` the cell is coloured with, and the first completion day. Cells of applets the participant is not in have status `not_in_applet`. |
| `dashboard_grid.csv` | The grid as shown, one column per cell, for reading rather than analysis. |
| `participants.csv` | One row per participant: nickname, tag, applets, the day they were added, default or individual schedule, start date and its source (rolling view), totals, adherence, last completion, next due. In a focused timeline this is `activities.csv`, one row per activity. |
| `occurrences.csv` | One row per expected occurrence and per completion of it: participant, applet, activity, day, time window, status, schedule (default or individual), periodicity, completion times, whether it was in the window, submission ID. The same content as the click-through details. |
| `dashboard.xlsx` (optional, on by default) | The same content as one Excel workbook: an **Info** sheet (the metadata and the status legend), a **Grid** sheet coloured as in the app, and one sheet per CSV with numbers as numbers, frozen headers, and filters. Needs the `openpyxl` package, which installs with the tool; if it is missing the workbook is skipped with a warning. |
| `dashboard_grid.json` (optional, on by default) | The grid exactly as the page renders it: columns, rows with cells `{e, d, l, p, m, u, x, s, t}`, totals, legend, notes, and the view parameters. For re-rendering or further processing without the CSVs. |

**Quick CSV of this grid** downloads `dashboard_grid.csv` alone through the browser.

### Trying it without a server

```bash
.venv/bin/python -m tests.demo_server
```

runs the app against a fake Curious with a scheduled study (log in with `researcher@example.org` /
`account-pw`, any server address); nothing leaves your computer.

## Data dictionaries

A data dictionary describes the items (questions) of each activity: prompt, type, response
options, scores, skip logic, and more. You can download either of two kinds:

- **All saved versions** (`--all-versions`, the default in scripts) shows how each question changed
  over the course of a study. It needs one request per saved version.
- **Only the current version** (`--current-only`) is one request per applet. Every row is the item
  as it is now.

In the wizard, choose **Data dictionary** at the first question. Then pick a workspace, either all of
its applets or some of them, and which versions. In scripts:

```bash
curious-download --dictionary --no-input --server https://<curious-api-address> \
  --email you@example.org --applet "EMA Study" --applet "Sleep Study"
curious-download --dictionary --no-input --server https://<curious-api-address> \
  --email you@example.org --all-applets --workspace "My Lab" --current-only
```

Both kinds use the same columns. With `--current-only`, `first_version`, `last_version`, and
`applet_versions` all give the current version, `status` is `current`, and `change` is empty. Only
activities that still exist are included. `export_info.json` records which kind you chose
(`"versions": "all"` or `"current"`).

No applet password is needed, because nothing is decrypted and no answers are downloaded. Any
role on an applet works, including editor and coordinator.

Each run writes a folder `<output>/data_dictionary_<applet or workspace>_<timestamp>/` containing:

- **`<applet>__<activity>__data_dictionary.csv`**: one file per activity. With all versions,
  activities deleted in a later version are included, because answers to them stay on the server. If two activities would
  get the same file name, their IDs are added to it.
- **`index.csv`**: one row per file, with the number of items, how many of them changed, the first
  and last version, the status (`current` or `removed in <version>`), and notes such as
  `reviewer assessment`. Applets that couldn't be read are listed here with the reason.
- **`export_info.json`**: the tool version, server, the version count of each applet, and in
  `command` the `curious-download --dictionary …` command that repeats this download.

### Rows and versions

Each row is one **definition** of an item. An item that never changed has one row. When an edit
changes it, a new row starts:

| item_name | first_version | last_version | status | change |
|---|---|---|---|---|
| mood | 1.0.0 | 1.0.1 | changed in 1.1.0 | |
| mood | 1.1.0 | 2.0.0 | current | response options; scores |
| energy | 1.1.0 | 2.0.0 | current | added |
| sleep_hours | 1.0.0 | 1.1.0 | removed in 2.0.0 | |

`applet_versions` lists every version a row covers. To find the definition that applies to an
answer, match the answer's `applet_version` in responses.csv with this list, and its `item_id` (or
`item_name`) with the row's. Items are followed by their ID, so a renamed item stays one item
(`renamed (was …)`).

| Column | Meaning |
|---|---|
| `activity_flows` | Flows that included the activity while this definition was in use |
| `item_order` | The item's position in the activity, in the last version the row covers |
| `item_id`, `item_name`, `item_type` | As in responses.csv |
| `item_prompt` | The question text, in English if the applet has it, otherwise its first language |
| `item_response_options` | Exactly the `item_response_options` cell responses.csv has for this item |
| `value_labels` | `value=label` for each option, e.g. `0=Never \| 1=Sometimes`. Sliders: the labelled ends. Grids: the column options |
| `scores` | `value=score` for scored items; for grids, per row |
| `min_value`, `max_value`, `min_label`, `max_label` | Sliders and number selections |
| `rows` | Grid rows; for slider grids, with their range and end labels |
| `response_rules` | Limits and settings that shape the answer: maximum characters, numbers only, correct answer, response required, audio length, time limit, random option order, continuous slider |
| `other_text_option` | `optional` or `required` when the item has an extra free-text box |
| `skippable` | `yes` or `no`; blank for items without a response, such as messages |
| `hidden` | Hidden items are not shown to participants and are not in responses.csv |
| `shown_if` | Skip logic, e.g. `mood = "Often" (1) AND sleep > 6` |
| `first_version`, `last_version`, `applet_versions` | The applet versions in which this definition was in effect |
| `status` | `current`, `changed in <version>` (the next row starts there), or `removed in <version>` |
| `change` | How the row differs from the item's previous row. Blank for an item's first row if it existed when the activity was created; otherwise one or more of: `added`, `added again`, `renamed (was …)`, `item ID changed (was …)`, `item type`, `prompt`, `response options`, `scores`, `alerts`, `appearance` (colors, images, tooltips), `skip logic`, `now hidden`, `no longer hidden`, `settings: …` |

After the items come the **score rows**, named like the score columns of responses.csv:
`activity_score`, `activity_score_lookup_text`, `subscale_name_<name>`, and
`subscale_lookup_text_<name>`. Their `item_type` is `total_score`, `subscale_score`, or
`lookup_text`. `item_prompt` says how the score is computed (e.g. `Subscale Anxiety: Average of q1,
q2`), and `value_labels` holds the lookup table (e.g. `raw 0~4 -> 50 (sex F, age 8~12, Mild):
Typical`). They follow the admin's rules: score columns exist only when the activity has at least
one subscale, and hidden items are left out of the total.

Notes:

- The dictionary needs one request per version. The admin panel saves a new version every time an
  applet is changed, so an often-edited applet can have hundreds of versions and take a minute or
  two.
- Every saved version is covered, including versions that never collected data.
- A change to any translation of a prompt counts as a `prompt` change, even if the English text
  shown in the file stayed the same.
- The data comes from `GET /applets/{id}/versions` and `GET /applets/{id}/versions/{version}`. The
  admin panel doesn't use the second endpoint. It returns item settings as they were stored, usually
  in snake_case (`min_value`) rather than the camelCase (`minValue`) used elsewhere in the API, so the
  tool accepts both.

## Converting a REDCap data dictionary

```bash
curious-download --convert-redcap "Cohort 4 Academy(Pre).csv"
```

This works offline: nothing is sent anywhere. The result is one data dictionary CSV per REDCap form,
in the same format as a downloaded dictionary, so you can check and edit it before
[uploading](#uploading-a-data-dictionary-into-an-applet). At the end the wizard offers to upload it
straight away.

Wherever the two platforms differ, it asks what to do. It only asks about what your file actually
contains, and the first answer offered is the recommended one:

| Situation in REDCap | Choices |
|---|---|
| Several forms | Which forms to convert (all of them by default). Staff-only forms can be left out |
| Rows with a label but no variable name (often a long label split over two rows) | Add the text to the label above, or leave them out |
| Matrix groups | One item per field (keeps every variable name and coded value), or one Curious grid per group, or decide per group |
| Section headers | A separate message screen before the field, added to the field's prompt (every field of a matrix group), or left out |
| Branching logic Curious can't express exactly | Keep the conditions Curious can express, show the field always, leave the field out, or decide per field. See below |
| Hidden fields (`@HIDDEN`, `@HIDDEN-SURVEY`) | Leave them out (nobody can fill in a hidden Curious item), or keep them hidden |
| Read-only fields (`@READONLY`), often feedback such as "Incorrect, review the video" | A message screen with the label (and the `@DEFAULT` value), an ordinary question, or left out |
| Sliders (REDCap 0–100, Curious at most 0–12) | A number picker with the same range, a 0–10 slider (different scale), or a number text box. Sliders whose range fits are converted directly |
| Whole-number fields with a small range | A number picker for the range, or a number text box |
| Date-time fields | A date item, or text |
| Notes boxes | Paragraph text, or text |
| File and signature fields | Photo, video, audio, drawing, or left out |
| Choice codes that aren't whole numbers | Renumber from 0 or from 1 (the report lists the new codes), or leave the field out |
| Calculated fields that add up, average, or count answers | Make them Curious subscales (see below), or leave them out |
| Options have no scores | No scores, or score each option with its code (items used by a subscale are always scored) |
| Form names | The Curious activity name (default: `student_pre_assessment` → `Student Pre Assessment`) |

It also converts automatically:
- **Field types:** radio, dropdown, yes/no, and true/false become single choice; checkboxes become
  multiple choice; descriptive fields become message screens.
- **Validation and requirements:** number validation becomes "numbers only", dates and times
  become date and time items, and required fields become non-skippable.
- **Text:** HTML labels become Markdown, including headings, lists, and tables. Piping like `[age]`
  becomes Curious's `[[age]]`, and field notes are added to the prompt.
- **Annotations:** `@NONEOFTHEABOVE`, `@HIDECHOICE`, `@SHOWCHOICE`, `@RANDOMORDER`, and `@CHARLIMIT`
  are converted.
- **Choices without a label:** a lone checkbox with no label (a REDCap "I agree" box) gets the
  label "Yes".

Things Curious can't do are reported, not silently dropped:
- Other calculated fields (`calc` with dates, conditions, or thresholds on other scores, and
  `@CALCTEXT`) are left out.
- Conditional annotations (`@IF(…)`) are ignored as a whole, so their choices are all shown.
- Tags that fill in values, such as `@DEFAULT` and `@NOW`, have no effect.
- Videos and files attached to descriptive fields aren't in the data dictionary. Each screen that
  mentions a video is listed, so you can add it in the admin panel.
- Also reported: matrix ranking, "check all that apply" on a single-choice field, and validation
  types such as email.

**Branching logic.** Curious combines an item's conditions with either AND or OR, and conditions can
only refer to earlier items in the same activity. When REDCap's logic is a plain list of conditions,
it converts exactly; a comparison like `[q1] > 2` on a single-choice field becomes
`q1 = 3 OR q1 = 4 …`. When it can't be converted exactly, the recommended choice keeps the
conditions Curious can express, so the field never stays hidden when REDCap would show it. For
example:

- `([attendance(1)] = '1' or [attendance(2)] = '1') and [intervention] = '6' and [session] = '1'`
  becomes `intervention = … AND session = …`. The attendance condition can't be combined with the
  others, so it is dropped.
- `([a] = '4' or [a] = '5') and ([b] = '1' or [b] = '2')` keeps one of the two groups,
  `a = 4 OR a = 5`.
- `datediff([dob], 'today', 'y') >= 18 and [consent] = '1'` keeps `consent = 1`.

The field may then show in more cases than in REDCap. Each simplified field is listed in the report
with the original logic, so you can edit its `shown_if` by hand. Fields whose logic Curious can't
express at all are shown always. Examples are logic that counts answers with `if()`, or that depends
on a field in another form or a calculated field.

Each run writes `<output>/redcap_<file>_<timestamp>/` with:

- **`<file>__<activity>__data_dictionary.csv`**: one per form.
- **`conversion_report.csv`**: every decision and every mismatch, field by field.
- **`redcap_decisions.json`**: your answers. Pass it with `--decisions` to convert again, or to
  convert a similar file (for example the Post survey), without the questions.
- **`export_info.json`**: the source, item counts, and decisions.

```bash
curious-download --convert-redcap "Cohort 4 Academy(Post).csv" \
  --decisions curious_exports/redcap_Cohort_4_Academy_Pre_<timestamp>/redcap_decisions.json --no-input
```

**Calculated fields.** Three kinds of REDCap calculation become Curious subscales, named after the
REDCap field. The subscale's column in responses.csv is `subscale_name_<field>`.

- **Sums:** `[gad7_1] + [gad7_2] + …` or `sum([a], [b])`. The items are scored with their REDCap codes,
  so Curious adds up the same values.
- **Averages:** `mean([a], [b])` or `([a] + [b] + [c]) / 3`.
- **Counts:** `if([asrs6_1] >= 2, 1, 0) + if([asrs6_2] >= 3, 1, 0) + …`. Each item is scored 1 or 0 by
  its threshold.

A calculation may use another converted calculation, which becomes a nested subscale. An item has
one set of scores, so a calculation that would score an item differently than another subscale
already does is left out and reported. Rounding is not kept (Curious shows 2 decimals).

Unanswered items differ between the two platforms:
- **REDCap:** `[a] + [b]` gives no result when an item is unanswered, and `mean()` averages only the
  answered items.
- **Curious:** unanswered items count as 0.

Before writing the files, the converter checks the result the way an upload will. If anything would
be refused, it is listed as a warning in the report.

## Uploading a data dictionary into an applet

```bash
curious-download --upload <file.csv or folder> --applet "EMA Study"
```

The applet must already exist (create it in the admin panel), and you need an owner, manager, or
editor role on it. No applet password is needed. The file is a data dictionary: one you downloaded
with `--dictionary` and edited, or one the REDCap converter wrote. A folder uploads every dictionary
CSV in it.

For each activity in the file:

- **If the applet has an activity with that name** (or `activity_id`), its items are updated:
  - Items are matched by `item_id` or `item_name`. Only the cells you changed are applied. Settings
    the dictionary doesn't show (images, colours, alerts, other languages) are kept.
  - New rows become new items.
  - Items in the applet but not in the file are listed. You choose whether to keep them (in their
    place) or remove them. With `--no-input` they are kept unless you pass `--remove-missing`.
- **Otherwise a new activity is created.**
- **Activities not in the file are left as they are.** The upload never removes an activity,
  because that would delete its schedule.

Which cells are read: `item_name`, `item_type`, `item_prompt`, `value_labels`, `scores`,
`min_value`/`max_value`, `min_label`/`max_label`, `rows`, `response_rules`, `other_text_option`,
`skippable`, `hidden`, and `shown_if`. They are written the same way as in a downloaded dictionary.
`item_response_options` is ignored; edit `value_labels` and `scores` instead. History rows (status
`changed in …` or `removed in …`) are skipped. `shown_if` uses `AND` or `OR` (capitals; one of them
per item) and can only refer to earlier items, e.g. `mood = "Often" (1) AND sleep > 6`.

**Subscales and the total score** are read from the score rows, written as a download writes them:

| item_name | item_type | item_prompt | value_labels |
|---|---|---|---|
| `subscale_name_Anxiety` | `subscale_score` | `Subscale Anxiety: Sum of q1, q2, q3` | `raw 0~4 -> 50 (sex F, age 8~12, Mild): Typical \| raw 5~9 -> 60` |
| `subscale_name_Total` | `subscale_score` | `Subscale Total: Average of Anxiety (subscale), q4` | |
| `activity_score` | `total_score` | `Total score: Sum of …` (the items are worked out by Curious) | `raw 0~10: Low` |

- `Sum of`, `Average of`, and `Sum of … as a percentage of its maximum` are the three scorings.
- `value_labels` holds the optional lookup table.
- A subscale that uses another subscale lists it with ` (subscale)`.
- Lookup-text rows (`…_lookup_text_…`) are written by downloads for information and ignored on upload.

How score rows are applied:
- **Unchanged rows:** a subscale whose row is unchanged is kept exactly as it is.
- **Subscales not in the file:** treated like missing items, so they are kept unless you remove them.
- **No score rows at all:** the activity's subscales are left alone.
- **Scoring needed:** the items a subscale uses must be choice or slider items with scores.

Before anything is sent:

1. The tool checks everything the backend would refuse, and stops with a list of problems if there
   are any. Examples: skip logic that refers to a later item, a slider beyond 0–12, a missing score,
   or an item still used by a subscale or report.
2. It shows what will change and asks for confirmation. With `--no-input` it is a dry run unless
   you add `--yes`.
3. Just before saving, it checks the applet hasn't changed on the server since it was read (someone
   saving in the admin panel at the same time). If it has, nothing is saved.

Saving replaces the whole applet, as the admin panel does, so everything the file doesn't cover is
sent back exactly as it was: other activities, flows, encryption, theme, and report settings. Each
upload creates a new applet version and sends participants an "Applet is updated" notification.
Re-uploading a dictionary you didn't change does nothing.

Each run writes `<output>/upload_<applet>_<timestamp>/` with:
- `upload_plan.csv`: every item, with the action and what changed.
- `upload_problems.csv`
- `applet_before.json`: the applet as it was.
- `applet_after.json`
- `upload_body.json`: exactly what was sent.
- `export_info.json`

To undo an upload, put the applet back as it was. Restoring removes activities the upload created;
activities it removed come back, but without their old schedule:

```bash
curious-download --restore curious_exports/upload_EMA_Study_<timestamp>/applet_before.json
```

## Participant counts per applet

`curious-participant-counts` is installed with the same package. It goes through every applet in a
workspace and counts the participants who have submitted data to each one:

```bash
curious-participant-counts                      # asks for the server, login, and workspace
curious-participant-counts --no-input --server https://<curious-api-address> \
  --email you@example.org --workspace "My Lab" --from 2026-09-01 --to 2026-09-30
```

It prints a table and writes `<output>/participant_counts_<workspace>_<timestamp>.csv`, one row per
applet:

| Column | Meaning |
|---|---|
| `participants_submitted` | People who completed at least one activity (the respondents) |
| `participants_with_data` | Participants the answers are **about** (target subjects). Differs from the above only when someone answers about another person, e.g. a parent about a child |
| `submissions` | Completed activities |
| `first_submission_utc`, `last_submission_utc` | When the server received the first and latest submission |
| `your_role`, `note` | Your role on the applet, and why an applet has no counts or partial counts |

- It reads submission metadata only (`GET /answers/applet/{id}/submissions`). No answers are
  downloaded or decrypted, so no applet passwords are needed. Older backends without that endpoint
  are detected automatically, and the tool falls back to the export endpoint. That is slower
  because it transfers the encrypted answers, though it still never decrypts them.
- Reviewer assessments (answers to reviewer-only activities) are not counted.
- You need an owner, manager, or reviewer role on an applet to count it. Other applets are listed
  with a note. As a reviewer you only see, and so only count, the participants assigned to you.
- `--from` / `--to` work as in `curious-download` (local time, compared with the time the server
  received each submission).
- Participants removed from an applet are still counted if their data was kept, as their
  submissions are still on the server.
- Only the current version's reviewer-only activities are excluded, so assessments for an
  activity that has since been deleted from the applet would be counted.

## Security

- Passwords are never written to disk. `~/.config/curious-download/settings.json` only remembers
  the server address and email.
- **Exports contain decrypted participant data**, and media files (photos, voice recordings, video)
  are often the most identifying part. Store them as you would any study data.
- Media downloads need an owner, manager, or reviewer role on the applet; the server refuses to
  sign links for anyone else.
- Use `CURIOUS_PASSWORD` / `CURIOUS_APPLET_PASSWORD` for scripts instead of putting passwords on
  the command line, where they end up in shell history.

## Not yet included

These are produced by the admin export but not by this tool yet:

- The per-task CSVs for drawing, AB Trails, stability tracker, and flanker.
- EHR data.

Also, `--activity` only matches activities in the current applet version. Answers to deleted
activities are still exported when no activity filter is set.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/pytest
```

Node.js is needed for two kinds of test:

- `tests/js_oracle/admin_export.js` holds the admin panel's export functions, copied with only
  the TypeScript types removed. The parity tests run them and the Python port on the same
  synthetic submissions (`tests/corpus.py`, every item type and the known edge cases) and
  require identical cells and column order (for `responses.csv` and `activity_user_journey.csv`),
  and identical media file names and URLs.
- `tests/js_oracle/crypto_oracle.js` encrypts answers with Node's crypto exactly as the mobile
  app does. The Python side must decrypt them.

`tests/test_cli.py` runs the whole command against an in-memory fake server: MFA, token refresh,
paging, filters, and the files written. `tests/test_webapp.py` runs the web app's API against the
same fake servers: login and MFA, applet unlocking, all three downloads end to end, and the
app-token check.

The upload tests (`tests/test_upload.py`) use a fake server that saves applets the way the backend
does: everything is replaced and new IDs are given out. The applet they edit (`tests/full_applet.py`)
has the shape the admin Builder loads, with every item type and condition type.

`tests/test_backend_schema.py` goes further: it validates upload bodies with the backend's own
request model (`AppletUpdate`, from the local `mindlogger-backend-refactor-develop`). That covers
item settings, skip logic, scores, and subscales. It runs when `CURIOUS_BACKEND_PYTHON` points to a
Python that has the backend's packages, and is skipped otherwise:

```bash
python -m venv /tmp/backend-venv
/tmp/backend-venv/bin/pip install "pydantic[email]" pydantic-extra-types "nh3==0.3.*" requests starlette fastapi pydantic-settings
CURIOUS_BACKEND_PYTHON=/tmp/backend-venv/bin/python .venv/bin/pytest tests/test_backend_schema.py
```
