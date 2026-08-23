# Instagram Unfollow Alert

A small Python script that checks an Instagram account's followers once every 24 hours and sends the result to an [ntfy](https://ntfy.sh/) topic.

## How It Works

On startup, the script:

1. Loads an existing Instaloader session for the Instagram username.
2. Reads the previous follower baseline from `followers.csv`.
3. Fetches the account's current followers.
4. Sends either a list of accounts that no longer follow you or `No unfollows` to ntfy.
5. If an unfollow is detected, updates `followers.csv` with the current follower list.
6. Waits 24 hours and repeats the check.

The CSV contains one Instagram username per row, for example:

```csv
username_one
username_two
username_three
```

## Requirements

- Python 3
- An Instagram account and an Instaloader session file
- An ntfy topic

Install the dependencies with:

```bash
python3 -m pip install -r requirements.txt
```

## Instaloader Session

The script expects an Instaloader session file at:

```text
/root/.config/instaloader/session-<USERNAME>
```

Create the session before running the monitor. For example:

```bash
instaloader --login <USERNAME>
```

If Instaloader stores the session elsewhere, update `load_session()` in `main.py` to use that path.

## Configuration

Set these environment variables before starting the script:

| Variable | Description |
| --- | --- |
| `USERNAME` | Instagram username whose followers should be checked |
| `TOPIC` | ntfy topic name used for notifications |

Example:

```bash
export USERNAME="your_instagram_username"
export TOPIC="your_private_ntfy_topic"
python3 main.py
```

Subscribe to the same topic in the ntfy app or at `https://ntfy.sh/<TOPIC>` to receive notifications.

## Running Continuously

The program runs indefinitely and sleeps for 24 hours between checks. Run it in a process manager, container, or persistent terminal session so it stays available.

## Important Notes

- The Instagram session must already be authenticated before running the script.
- `TOPIC` should be difficult to guess because ntfy topics are publicly addressable unless protected separately.
- `followers.csv` is used as the comparison baseline and must be populated before the first run.
- When an unfollow is detected, the script automatically replaces `followers.csv` with the current follower list, preventing the same unfollow from being reported again on the next check.
- When no unfollows are detected, the existing CSV baseline is left unchanged.
- Instagram or Instaloader rate limits and authentication failures can prevent a check from completing.

## License

No license has been specified for this project.