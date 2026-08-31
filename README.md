# Instagram Unfollow Alert

This script opens your Instagram profile in a persistent CloakBrowser session,
clicks **followers**, scrolls the followers window to its verified end, collects
the usernames, and compares them with `followers.csv`.

It prints new followers and unfollows. If an ntfy topic is configured, unfollow
alerts and failures are also sent there.

## Safety

`followers.csv` is replaced only after a complete scrape. The script requires
the real modal scroller to be at the bottom with stable content and, when
Instagram exposes a follower count, verifies that at least that many usernames
were collected. Instagram's profile counter can briefly lag behind its follower
rows; a larger verified modal result is kept with a warning instead of dropping
a real username. An empty or partial scrape therefore cannot erase a valid
baseline or create a mass false-unfollow alert.

The CSV is written atomically and contains one normalized username per row:

```csv
username_one
username_two
username_three
```

## Install

Requirements:

- Python 3.9 or newer
- The Instagram account whose followers are being monitored
- A graphical session for the first login

Install the Python dependencies:

```bash
python3 -m pip install -r requirements.txt
```

CloakBrowser downloads its Chromium binary on first use. The browser profile is
stored in `cloak_profile/`, so the Instagram login survives later runs.

## First Run and Login

Set the account whose profile should be checked. A leading `@` is accepted.

```bash
export INSTAGRAM_USERNAME="your_instagram_username"
export INSTAGRAM_LOGIN_WAIT_SECONDS=300
python3 main.py
```

If Instagram asks you to log in, complete the login in the opened browser. The
script waits up to five minutes and then continues to the profile. After that,
the persisted session normally lets you omit `INSTAGRAM_LOGIN_WAIT_SECONDS`.
The saved browser session must be signed into the same account named by
`INSTAGRAM_USERNAME`; otherwise Instagram can expose only part of that account's
follower list. If another account is active, the script waits for you to switch
accounts and open the monitored account's profile during this first-run window.

`scraping_method.py` remains available as a compatibility entrypoint and runs
the same implementation.

## Comparison Behavior

- If `followers.csv` does not exist, the first complete scrape creates it as the
  initial baseline.
- On later runs, `old - current` is reported as unfollows and `current - old` is
  printed as new followers.
- Every complete run refreshes the CSV, including runs with additions only.
- A failed, timed-out, logged-out, or incomplete scrape leaves the existing CSV
  untouched.

## Optional Notifications

Set an ntfy topic to receive unfollow alerts and scraper failures:

```bash
export NTFY_TOPIC="your_private_ntfy_topic"
```

Use a difficult-to-guess topic unless your ntfy server protects it. The legacy
variables `NFTY_TOPIC_INSTAGRAM` and `TOPIC` are also accepted.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `INSTAGRAM_USERNAME` | required | Instagram profile to check; `USERNAME` is also accepted |
| `NTFY_TOPIC` | unset | Optional ntfy topic |
| `INSTAGRAM_LOGIN_WAIT_SECONDS` | `0` | Time allowed for an interactive login |
| `INSTAGRAM_HEADLESS` | `false` | Run without a visible browser after login is established |
| `INSTAGRAM_PROFILE_PATH` | `cloak_profile/` | Persistent browser profile directory |
| `FOLLOWERS_CSV` | `followers.csv` | Baseline CSV path |
| `INSTAGRAM_SCROLL_WAIT_MS` | `2000` | Lazy-load wait after each modal scroll |
| `INSTAGRAM_STABLE_BOTTOM_ROUNDS` | `5` | Required unchanged checks at the real bottom |
| `INSTAGRAM_MAX_SCROLL_ROUNDS` | `2000` | Hard scroll-round limit |
| `INSTAGRAM_STALLED_BOTTOM_RETRIES` | `5` | Back-off retries when the modal stalls below the profile count |
| `INSTAGRAM_SCRAPE_TIMEOUT` | `3600` | Overall scrape timeout in seconds |

Relative profile and CSV paths are resolved from the repository directory, not
the shell's current directory. This prevents a scheduled job from accidentally
creating a second baseline somewhere else.

If Instagram does not expose an exact count, the run stops without changing the
baseline. Inspect the visible browser and retry; unknown-count results are never
used as a new baseline.

When an unfollow alert is due and ntfy delivery fails, the CSV is deliberately
left unchanged so the alert can be retried on the next run.

## Run Regularly

The command performs one check and exits. Schedule it with cron, launchd,
systemd, or another job runner. Start with headed mode; once the saved session
is working, `INSTAGRAM_HEADLESS=1` can be used if Instagram accepts it.

## Tests

The offline test suite covers CSV integrity, href filtering, set comparison,
partial-scrape rejection, atomic replacement, and delayed modal loading:

```bash
python3 -B -m unittest discover -s tests -v
```
