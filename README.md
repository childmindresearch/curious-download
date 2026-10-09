# curious-download

Download and decrypt Curious (MindLogger) answer data from the command line, filtered by
participant, date range, and activity. The output is a `responses.csv` in the same format as
the admin panel's **Export data** (renamed-column version), so the existing
`mindlogger-data-export` post-processing tool and transformation scripts can read it.

The same command also downloads [data dictionaries](#data-dictionaries): one CSV per activity
describing its items across every version of the applet.

Prefer point-and-click? [`curious-app`](#the-app-curious-app) runs the same downloads
(answers, data dictionaries, and participant counts) from a page in your browser.

## Install

Requires Python 3.11+.

```bash
pip install -e /path/to/curious-data-download
```

or with uv:

```bash
uv tool install /path/to/curious-data-download
```

## Use

### Interactive

Run it with no options and answer the questions:

```bash
curious-download
```

It first asks whether you want answers or a data dictionary. For answers, it then asks for the API
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
  any submissions that could not be decrypted. It contains no passwords.

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
- **Data dictionary** — choose a workspace and all or some of its applets.
- **Participant counts** — choose a workspace and a date range; the table is shown in the page
  and saved as a CSV.

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

## Data dictionaries

A data dictionary describes the items (questions) of each activity: prompt, type, response
options, scores, skip logic, and more. Every saved version of the applet is included, so changes
to a question over the course of a study are visible.

In the wizard, choose **Data dictionary** at the first question, then pick a workspace and either
all of its applets or some of them. In scripts:

```bash
curious-download --dictionary --no-input --server https://<curious-api-address> \
  --email you@example.org --applet "EMA Study" --applet "Sleep Study"
curious-download --dictionary --no-input --server https://<curious-api-address> \
  --email you@example.org --all-applets --workspace "My Lab"
```

No applet password is needed, because nothing is decrypted and no answers are downloaded. Any
role on an applet works, including editor and coordinator.

Each run writes a folder `<output>/data_dictionary_<applet or workspace>_<timestamp>/` containing:

- **`<applet>__<activity>__data_dictionary.csv`**: one file per activity. Activities deleted in a
  later version are included, because answers to them stay on the server. If two activities would
  get the same file name, their IDs are added to it.
- **`index.csv`**: one row per file, with the number of items, how many of them changed, the first
  and last version, the status (`current` or `removed in <version>`), and notes such as
  `reviewer assessment`. Applets that couldn't be read are listed here with the reason.
- **`export_info.json`**: the tool version, server, and the version count of each applet.

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
