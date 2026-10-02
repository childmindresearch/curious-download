# curious-download

Download and decrypt Curious (MindLogger) answer data from the command line, filtered by
participant, date range, and activity. The output is a `responses.csv` in the same format as
the admin panel's **Export data** (renamed-column version), so the existing
`mindlogger-data-export` post-processing tool and transformation scripts can read it.

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

It asks for the API address, your email and password (plus a 6-digit code if your account uses
MFA), then the applet and its applet password. Then you choose participants, a date range, and
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
| `--participant`, `-p` | Participant secret ID (repeatable). Matches the participant the answers are **about** (target subject), as in the admin panel's per-participant export |
| `--from`, `--to` | Dates (`YYYY-MM-DD`) or datetimes in **your local time**; converted to UTC and compared with the time the server received each answer |
| `--activity`, `-a` / `--flow` | Activity or flow name or ID (repeatable) |
| `--media` | Also download the media files of the selected answers (photos, video, audio, drawings, Unity task files) |
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

- `activity_user_journey.csv`.
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
  require identical cells and column order, and identical media file names and URLs.
- `tests/js_oracle/crypto_oracle.js` encrypts answers with Node's crypto exactly as the mobile
  app does. The Python side must decrypt them.

`tests/test_cli.py` runs the whole command against an in-memory fake server: MFA, token refresh,
paging, filters, and the files written.
