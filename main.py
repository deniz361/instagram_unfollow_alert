from __future__ import annotations

import csv
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence, Set, Tuple, Union
from urllib.parse import unquote, urlsplit


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PROFILE_PATH = BASE_DIR / "cloak_profile"
DEFAULT_FOLLOWERS_PATH = BASE_DIR / "followers.csv"

USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
GROUPED_INTEGER_RE = re.compile(r"^(?:\d+|\d{1,3}(?:[.,\s]\d{3})+)$")
FOLLOWER_COUNT_RE = re.compile(
    r"(?P<count>\d+|\d{1,3}(?:[.,\s]\d{3})+)\s+followers?\b",
    re.IGNORECASE,
)
FOLLOWERS_LABEL_RE = re.compile(r"\bfollowers?\b", re.IGNORECASE)
DIALOG_MARKER = "data-instagram-follower-scraper-existing"

NON_PROFILE_ROUTES = {
    "about",
    "accounts",
    "challenge",
    "developer",
    "direct",
    "directory",
    "emails",
    "explore",
    "legal",
    "p",
    "privacy",
    "reels",
    "stories",
    "terms",
    "tv",
    "web",
}

SCROLL_DIALOG_JAVASCRIPT = r"""
dialog => {
    const candidates = [dialog, ...dialog.querySelectorAll("*")]
        .filter(element => {
            if (!(element instanceof HTMLElement)) return false;
            return element.clientHeight > 0 &&
                element.scrollHeight > element.clientHeight + 2;
        })
        .map(element => {
            const style = getComputedStyle(element);
            return {
                element,
                preferred: style.overflowY === "auto" ||
                    style.overflowY === "scroll",
                links: element.querySelectorAll("a[href]").length,
                range: element.scrollHeight - element.clientHeight,
            };
        });

    const preferred = candidates.filter(candidate => candidate.preferred);
    const pool = preferred.length ? preferred : candidates;

    pool.sort((left, right) =>
        right.links - left.links || right.range - left.range
    );

    if (!pool.length) {
        return {
            found: false,
            before: 0,
            after: 0,
            clientHeight: dialog.clientHeight,
            scrollHeight: dialog.scrollHeight,
            atBottom: true,
        };
    }

    const scroller = pool[0].element;
    const before = scroller.scrollTop;
    const maximum = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
    const step = Math.max(240, Math.floor(scroller.clientHeight * 0.8));
    const target = Math.min(maximum, before + step);

    scroller.scrollTop = target;

    return {
        found: true,
        before,
        after: scroller.scrollTop,
        clientHeight: scroller.clientHeight,
        scrollHeight: scroller.scrollHeight,
        atBottom: scroller.scrollTop >= maximum - 2,
    };
}
"""

NUDGE_DIALOG_JAVASCRIPT = r"""
dialog => {
    const candidates = [dialog, ...dialog.querySelectorAll("*")]
        .filter(element => {
            if (!(element instanceof HTMLElement)) return false;
            return element.clientHeight > 0 &&
                element.scrollHeight > element.clientHeight + 2;
        })
        .map(element => {
            const style = getComputedStyle(element);
            return {
                element,
                preferred: style.overflowY === "auto" ||
                    style.overflowY === "scroll",
                links: element.querySelectorAll("a[href]").length,
                range: element.scrollHeight - element.clientHeight,
            };
        });

    const preferred = candidates.filter(candidate => candidate.preferred);
    const pool = preferred.length ? preferred : candidates;
    pool.sort((left, right) =>
        right.links - left.links || right.range - left.range
    );
    if (!pool.length) return false;

    const scroller = pool[0].element;
    const maximum = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
    scroller.scrollTop = Math.max(
        0,
        maximum - Math.max(160, Math.floor(scroller.clientHeight * 0.5))
    );
    return true;
}
"""

COLLECT_DIALOG_USERNAMES_JAVASCRIPT = r"""
dialog => {
    const candidates = [dialog, ...dialog.querySelectorAll("*")]
        .filter(element => {
            if (!(element instanceof HTMLElement)) return false;
            return element.clientHeight > 0 &&
                element.scrollHeight > element.clientHeight + 2;
        })
        .map(element => {
            const style = getComputedStyle(element);
            return {
                element,
                preferred: style.overflowY === "auto" ||
                    style.overflowY === "scroll",
                links: element.querySelectorAll("a[href]").length,
                range: element.scrollHeight - element.clientHeight,
            };
        });

    const preferred = candidates.filter(candidate => candidate.preferred);
    const pool = preferred.length ? preferred : candidates;
    pool.sort((left, right) =>
        right.links - left.links || right.range - left.range
    );

    const scroller = pool.length ? pool[0].element : dialog;
    return [...scroller.querySelectorAll("a[href]")]
        .filter(link => {
            const style = getComputedStyle(link);
            return style.display !== "none" &&
                style.visibility !== "hidden" &&
                Number(style.opacity || "1") !== 0 &&
                link.getClientRects().length > 0;
        })
        .map(link => [
            link.getAttribute("href"),
            (link.textContent || "").replace(/\s+/g, " ").trim(),
        ]);
}
"""


class IncompleteScrapeError(RuntimeError):
    """Raised when the follower list cannot be proven complete."""


@dataclass(frozen=True)
class ScrapeResult:
    followers: Set[str]
    expected_count: Optional[int]
    scroll_rounds: int
    reached_end: bool


def normalize_username(value: str) -> str:
    username = value.strip().lstrip("@").casefold()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError(f"Invalid Instagram username: {value!r}")
    return username


def username_from_profile_href(href: str) -> Optional[str]:
    if not href:
        return None

    parsed = urlsplit(href.strip())
    if parsed.scheme and parsed.scheme.casefold() not in {"http", "https"}:
        return None

    if parsed.netloc:
        host = (parsed.hostname or "").casefold()
        if host != "instagram.com" and not host.endswith(".instagram.com"):
            return None
    elif not parsed.path.startswith("/"):
        return None

    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) != 1:
        return None

    try:
        username = normalize_username(parts[0])
    except ValueError:
        return None

    if username in NON_PROFILE_ROUTES:
        return None
    return username


def extract_usernames(hrefs: Iterable[str]) -> Set[str]:
    return {
        username
        for href in hrefs
        if (username := username_from_profile_href(href)) is not None
    }


def extract_visible_username_links(
    links: Iterable[Sequence[str]],
) -> Set[str]:
    usernames: Set[str] = set()
    for link in links:
        if len(link) != 2:
            continue
        href, text = link
        username = username_from_profile_href(href)
        if username is None:
            continue

        label = text.strip()
        if label.startswith("@"):
            label = label[1:]
        if label.casefold() == username:
            usernames.add(username)
    return usernames


def load_followers(
    csv_path: Union[Path, str] = DEFAULT_FOLLOWERS_PATH,
) -> Optional[Set[str]]:
    path = Path(csv_path)
    if not path.exists():
        return None

    followers: Set[str] = set()
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        for line_number, row in enumerate(csv.reader(file), start=1):
            if not row or all(not value.strip() for value in row):
                continue
            if len(row) != 1:
                raise ValueError(
                    f"{path} line {line_number} must contain one username"
                )
            try:
                followers.add(normalize_username(row[0]))
            except ValueError as error:
                raise ValueError(f"{path} line {line_number}: {error}") from error
    return followers


def save_followers(
    followers: Iterable[str],
    csv_path: Union[Path, str] = DEFAULT_FOLLOWERS_PATH,
) -> None:
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = sorted({normalize_username(value) for value in followers})
    temporary_name: Optional[str] = None

    try:
        with tempfile.NamedTemporaryFile(
            "w",
            newline="",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_name = file.name
            csv.writer(file).writerows([[username] for username in normalized])
            file.flush()
            os.fsync(file.fileno())

        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def compare_followers(
    previous: Set[str], current: Set[str]
) -> Tuple[Set[str], Set[str]]:
    return previous - current, current - previous


def parse_expected_follower_count(values: Sequence[Optional[str]]) -> Optional[int]:
    cleaned_values = [
        value.replace("\xa0", " ").strip()
        for value in values
        if isinstance(value, str) and value.strip()
    ]

    for value in cleaned_values:
        if GROUPED_INTEGER_RE.fullmatch(value):
            return int(re.sub(r"\D", "", value))

    for value in cleaned_values:
        match = FOLLOWER_COUNT_RE.search(value)
        if match:
            return int(re.sub(r"\D", "", match.group("count")))
    return None


def _visible(locator: object) -> bool:
    try:
        return bool(locator.count() and locator.first.is_visible())
    except Exception:
        return False


def _login_required(page: object) -> bool:
    path = urlsplit(page.url).path.casefold()
    if path.startswith("/accounts/login"):
        return True
    return _visible(page.locator('input[name="username"]')) and _visible(
        page.locator('input[name="password"]')
    )


def _raise_for_challenge(page: object) -> None:
    path = urlsplit(page.url).path.casefold()
    if path.startswith(("/challenge", "/checkpoint")):
        raise RuntimeError(
            "Instagram requires a security check. Complete it in the browser "
            "before running the scraper again."
        )


def _wait_for_authentication(page: object, wait_seconds: float) -> bool:
    _raise_for_challenge(page)
    if not _login_required(page):
        return False
    if wait_seconds <= 0:
        raise RuntimeError(
            "Instagram login is required. Run once with "
            "INSTAGRAM_LOGIN_WAIT_SECONDS=300 and log in in the opened browser."
        )

    print(
        "Instagram login required. Log in in the opened browser; "
        f"waiting up to {wait_seconds:g} seconds..."
    )
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        _raise_for_challenge(page)
        if not _login_required(page):
            return True
        page.wait_for_timeout(1000)
    raise RuntimeError("Timed out waiting for Instagram login")


def profile_url_matches_username(url: str, username: str) -> bool:
    path_parts = [part for part in urlsplit(url).path.split("/") if part]
    return len(path_parts) == 1 and path_parts[0].casefold() == username.casefold()


def _own_profile_controls_visible(page: object, target_username: str) -> bool:
    if not profile_url_matches_username(page.url, target_username):
        return False
    edit_link = page.locator('header a[href*="/accounts/edit"]')
    if _visible(edit_link):
        return True
    return _visible(page.get_by_text("Edit profile", exact=True))


def _wait_for_target_account(
    page: object,
    target_username: str,
    wait_seconds: float,
) -> None:
    discovery_deadline = time.monotonic() + 5
    while time.monotonic() < discovery_deadline:
        if _own_profile_controls_visible(page, target_username):
            return
        _raise_for_challenge(page)
        page.wait_for_timeout(500)

    if wait_seconds <= 0:
        raise RuntimeError(
            "The persistent browser is signed into a different Instagram "
            "account. Run with INSTAGRAM_LOGIN_WAIT_SECONDS=300 and switch "
            "to the monitored account in the opened browser."
        )

    print(
        "The browser is signed into a different Instagram account. "
        f"Switch to @{target_username} and open that profile; waiting up to "
        f"{wait_seconds:g} seconds..."
    )
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        _raise_for_challenge(page)
        if _own_profile_controls_visible(page, target_username):
            return
        page.wait_for_timeout(1000)
    raise RuntimeError("Timed out waiting for the monitored Instagram account")


def _followers_link(page: object, username: str) -> object:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        exact = page.locator(f'a[href$="/{username}/followers/"]').first
        try:
            if exact.count() and exact.is_visible():
                return exact
        except Exception:
            pass

        for selector in ("header a[href]", "a[href]"):
            links = page.locator(selector)
            for index in range(links.count()):
                link = links.nth(index)
                try:
                    text = link.inner_text(timeout=250)
                    if link.is_visible() and FOLLOWERS_LABEL_RE.search(text):
                        return link
                except Exception:
                    continue
        page.wait_for_timeout(250)
    raise RuntimeError("Could not find the Followers control on the profile")


def _expected_count_from_link(link: object) -> Optional[int]:
    values = link.evaluate(
        r"""
        link => {
            const nodes = [link, ...link.querySelectorAll("[title], [aria-label]")];
            const values = [link.textContent];
            for (const node of nodes) {
                values.push(node.getAttribute("title"));
                values.push(node.getAttribute("aria-label"));
            }
            return values.filter(Boolean);
        }
        """
    )
    return parse_expected_follower_count(values)


def _mark_existing_visible_dialogs(page: object) -> None:
    all_dialogs = page.locator('div[role="dialog"]')
    all_dialogs.evaluate_all(
        "(dialogs, marker) => dialogs.forEach(dialog => "
        "dialog.removeAttribute(marker))",
        DIALOG_MARKER,
    )
    page.locator('div[role="dialog"]:visible').evaluate_all(
        "(dialogs, marker) => dialogs.forEach(dialog => "
        "dialog.setAttribute(marker, ''))",
        DIALOG_MARKER,
    )


def _wait_for_new_followers_dialog(
    page: object,
    expected_count: Optional[int],
    timeout_ms: int = 15000,
) -> object:
    dialogs = page.locator(
        f'div[role="dialog"]:visible:not([{DIALOG_MARKER}])'
    )
    deadline = time.monotonic() + timeout_ms / 1000

    while time.monotonic() < deadline:
        for index in range(dialogs.count()):
            dialog = dialogs.nth(index)
            try:
                if not FOLLOWERS_LABEL_RE.search(dialog.inner_text(timeout=500)):
                    continue
                if expected_count == 0 or _collect_dialog_usernames(dialog):
                    return dialog
            except Exception:
                continue
        page.wait_for_timeout(250)
    raise RuntimeError("Instagram did not open a verified Followers window")


def _open_followers_dialog(
    page: object,
    profile_url: str,
    username: str,
    login_wait_seconds: float,
) -> Tuple[object, Optional[int]]:
    for attempt in range(2):
        page.goto(
            profile_url,
            wait_until="domcontentloaded",
            timeout=60000,
        )
        login_was_required = _wait_for_authentication(
            page, login_wait_seconds
        )
        if login_was_required:
            page.goto(
                profile_url,
                wait_until="domcontentloaded",
                timeout=60000,
            )
        _wait_for_target_account(page, username, login_wait_seconds)
        _raise_for_challenge(page)

        link = _followers_link(page, username)
        expected_count = _expected_count_from_link(link)
        _mark_existing_visible_dialogs(page)
        link.click(timeout=15000)

        try:
            dialog = _wait_for_new_followers_dialog(page, expected_count)
        except Exception as error:
            if _login_required(page) and attempt == 0:
                _wait_for_authentication(page, login_wait_seconds)
                continue
            raise RuntimeError("Instagram did not open the followers window") from error

        if _login_required(page):
            if attempt == 0:
                _wait_for_authentication(page, login_wait_seconds)
                continue
            raise RuntimeError("Instagram login is required")
        return dialog, expected_count

    raise RuntimeError("Unable to open the followers window after login")


def _collect_dialog_usernames(dialog: object) -> Set[str]:
    links = dialog.evaluate(COLLECT_DIALOG_USERNAMES_JAVASCRIPT)
    return extract_visible_username_links(links)


def _scroll_to_end(
    page: object,
    dialog: object,
    expected_count: Optional[int],
    *,
    wait_ms: int,
    stable_bottom_rounds: int,
    max_scroll_rounds: int,
    max_stalled_retries: int,
    timeout_seconds: float,
) -> Tuple[Set[str], int]:
    followers: Set[str] = set()
    stable_rounds = 0
    previous_signature = None
    previous_reported_count = -1
    stalled_retries = 0
    missing_scroller_rounds = 0
    largest_count_seen = 0
    deadline = time.monotonic() + timeout_seconds

    for round_number in range(1, max_scroll_rounds + 1):
        if time.monotonic() >= deadline:
            raise IncompleteScrapeError(
                "Timed out before the followers window reached a stable end"
            )

        followers.update(_collect_dialog_usernames(dialog))
        if len(followers) > largest_count_seen:
            largest_count_seen = len(followers)
            stalled_retries = 0
        count_before_wait = len(followers)
        if count_before_wait != previous_reported_count:
            print(f"Followers loaded: {count_before_wait}")
            previous_reported_count = count_before_wait

        state = dialog.evaluate(SCROLL_DIALOG_JAVASCRIPT)
        page.wait_for_timeout(wait_ms)
        followers.update(_collect_dialog_usernames(dialog))
        if len(followers) > largest_count_seen:
            largest_count_seen = len(followers)
            stalled_retries = 0

        signature = (
            bool(state["found"]),
            int(state["after"]),
            int(state["clientHeight"]),
            int(state["scrollHeight"]),
        )
        unchanged = len(followers) == count_before_wait

        if not state["found"]:
            if unchanged and signature == previous_signature:
                missing_scroller_rounds += 1
            else:
                missing_scroller_rounds = 0
            previous_signature = signature

            if missing_scroller_rounds >= stable_bottom_rounds:
                if (
                    expected_count is not None
                    and len(followers) == expected_count
                ):
                    return followers, round_number
                raise IncompleteScrapeError(
                    "No scrollable follower list was found, and the visible "
                    "usernames could not be verified as complete"
                )
            continue

        missing_scroller_rounds = 0

        if state["atBottom"] and unchanged and signature == previous_signature:
            stable_rounds += 1
        else:
            stable_rounds = 0
        previous_signature = signature

        if stable_rounds >= stable_bottom_rounds:
            if expected_count is None or len(followers) >= expected_count:
                return followers, round_number

            stalled_retries += 1
            if stalled_retries > max_stalled_retries:
                raise IncompleteScrapeError(
                    f"The followers window stopped at {len(followers)} of "
                    f"{expected_count} reported followers after "
                    f"{max_stalled_retries} loading retries"
                )

            print(
                f"Follower loading stalled at {len(followers)} of "
                f"{expected_count}; retrying ({stalled_retries}/"
                f"{max_stalled_retries})..."
            )
            dialog.evaluate(NUDGE_DIALOG_JAVASCRIPT)
            page.wait_for_timeout(min(10000, wait_ms * (stalled_retries + 1)))
            followers.update(_collect_dialog_usernames(dialog))
            if len(followers) > largest_count_seen:
                largest_count_seen = len(followers)
                stalled_retries = 0
            stable_rounds = 0
            previous_signature = None
            continue

    raise IncompleteScrapeError(
        f"Followers window did not reach a stable end after {max_scroll_rounds} scrolls"
    )


def validate_scrape_result(
    result: ScrapeResult,
) -> None:
    if not result.reached_end:
        raise IncompleteScrapeError("The followers window did not reach its end")

    count = len(result.followers)
    if result.expected_count is not None and count < result.expected_count:
        raise IncompleteScrapeError(
            f"Instagram reports {result.expected_count} followers, but "
            f"{count} unique follower usernames were collected"
        )
    if count == 0 and result.expected_count != 0:
        raise IncompleteScrapeError(
            "No usernames were collected and a zero-follower account was not verified"
        )
    if result.expected_count is None:
        raise IncompleteScrapeError(
            "Instagram did not expose an exact follower count, so this scrape "
            "cannot be proven complete"
        )


def get_followers(
    username: str,
    *,
    profile_path: Union[Path, str] = DEFAULT_PROFILE_PATH,
    headless: bool = False,
    login_wait_seconds: float = 0,
    scroll_wait_ms: int = 2000,
    stable_bottom_rounds: int = 5,
    max_scroll_rounds: int = 2000,
    max_stalled_retries: int = 5,
    timeout_seconds: float = 3600,
) -> ScrapeResult:
    if login_wait_seconds < 0:
        raise ValueError("login_wait_seconds must be non-negative")
    if scroll_wait_ms < 0:
        raise ValueError("scroll_wait_ms must be non-negative")
    if stable_bottom_rounds < 1:
        raise ValueError("stable_bottom_rounds must be at least 1")
    if max_scroll_rounds < 1:
        raise ValueError("max_scroll_rounds must be at least 1")
    if max_stalled_retries < 0:
        raise ValueError("max_stalled_retries must be non-negative")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    try:
        from cloakbrowser import launch_persistent_context
    except ImportError as error:
        raise RuntimeError(
            "CloakBrowser is not installed. Run: "
            "python3 -m pip install -r requirements.txt"
        ) from error

    normalized_username = normalize_username(username)
    profile_url = f"https://www.instagram.com/{normalized_username}/"
    context = launch_persistent_context(
        str(profile_path),
        headless=headless,
        humanize=True,
    )

    try:
        page = context.pages[0] if context.pages else context.new_page()
        dialog, expected_before = _open_followers_dialog(
            page,
            profile_url,
            normalized_username,
            login_wait_seconds,
        )
        followers, scroll_rounds = _scroll_to_end(
            page,
            dialog,
            expected_before,
            wait_ms=scroll_wait_ms,
            stable_bottom_rounds=stable_bottom_rounds,
            max_scroll_rounds=max_scroll_rounds,
            max_stalled_retries=max_stalled_retries,
            timeout_seconds=timeout_seconds,
        )

        result = ScrapeResult(
            followers=followers,
            expected_count=expected_before,
            scroll_rounds=scroll_rounds,
            reached_end=True,
        )
        validate_scrape_result(result)
        return result
    finally:
        context.close()


def send_notification(message: str, topic: str) -> None:
    import requests

    response = requests.post(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers={"Title": "Instagram unfollows", "Priority": "high"},
        timeout=15,
    )
    response.raise_for_status()


def _environment_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    if value.casefold() in {"1", "true", "yes", "on"}:
        return True
    if value.casefold() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _configured_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    if not value:
        return default
    path = Path(value).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


def _notification_topic() -> Optional[str]:
    return (
        os.getenv("NTFY_TOPIC")
        or os.getenv("NFTY_TOPIC_INSTAGRAM")
        or os.getenv("TOPIC")
    )


def _report_and_notify(message: str, topic: Optional[str]) -> None:
    print(message)
    if topic:
        send_notification(message, topic)


def run() -> None:
    username = os.getenv("INSTAGRAM_USERNAME") or os.getenv("USERNAME")
    if not username:
        raise RuntimeError("Set INSTAGRAM_USERNAME to the account to check")

    followers_path = _configured_path("FOLLOWERS_CSV", DEFAULT_FOLLOWERS_PATH)
    profile_path = _configured_path(
        "INSTAGRAM_PROFILE_PATH", DEFAULT_PROFILE_PATH
    )
    previous = load_followers(followers_path)

    result = get_followers(
        username,
        profile_path=profile_path,
        headless=_environment_bool("INSTAGRAM_HEADLESS", False),
        login_wait_seconds=float(os.getenv("INSTAGRAM_LOGIN_WAIT_SECONDS", "0")),
        scroll_wait_ms=int(os.getenv("INSTAGRAM_SCROLL_WAIT_MS", "2000")),
        stable_bottom_rounds=int(
            os.getenv("INSTAGRAM_STABLE_BOTTOM_ROUNDS", "5")
        ),
        max_scroll_rounds=int(os.getenv("INSTAGRAM_MAX_SCROLL_ROUNDS", "2000")),
        max_stalled_retries=int(
            os.getenv("INSTAGRAM_STALLED_BOTTOM_RETRIES", "5")
        ),
        timeout_seconds=float(os.getenv("INSTAGRAM_SCRAPE_TIMEOUT", "3600")),
    )
    validate_scrape_result(result)

    if (
        result.expected_count is not None
        and len(result.followers) > result.expected_count
    ):
        print(
            "Warning: Instagram's profile counter reports "
            f"{result.expected_count}, but its completed followers window "
            f"contained {len(result.followers)} unique accounts. Using the "
            "followers window because profile counters can update later."
        )

    topic = _notification_topic()
    if previous is None:
        message = (
            f"Initial follower snapshot saved: {len(result.followers)} followers"
        )
        _report_and_notify(message, topic)
    else:
        unfollowed, new_followers = compare_followers(previous, result.followers)
        if unfollowed:
            _report_and_notify(
                "Unfollowed you:\n" + "\n".join(sorted(unfollowed)), topic
            )
        else:
            print("No unfollows")

        if new_followers:
            print("New followers:\n" + "\n".join(sorted(new_followers)))

    save_followers(result.followers, followers_path)
    print(
        f"Saved {len(result.followers)} usernames to {followers_path} "
        f"after {result.scroll_rounds} scroll rounds"
    )


def main() -> int:
    try:
        run()
        return 0
    except Exception as error:
        message = f"Instagram check failed:\n{error}"
        print(message, file=sys.stderr)
        topic = _notification_topic()
        if topic:
            try:
                send_notification(message, topic)
            except Exception as notification_error:
                print(
                    f"Warning: ntfy notification failed: {notification_error}",
                    file=sys.stderr,
                )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
