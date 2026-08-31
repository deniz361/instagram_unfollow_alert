import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main as scraper


class CsvTests(unittest.TestCase):
    def test_missing_csv_is_distinct_from_an_empty_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "followers.csv"
            self.assertIsNone(scraper.load_followers(path))

            path.write_text("", encoding="utf-8")
            self.assertEqual(scraper.load_followers(path), set())

    def test_load_normalizes_bom_whitespace_case_and_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "followers.csv"
            path.write_text(
                "\ufeffAlice \n\nBOB\nalice\n",
                encoding="utf-8",
            )
            self.assertEqual(scraper.load_followers(path), {"alice", "bob"})

    def test_load_rejects_extra_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "followers.csv"
            path.write_text("alice,unexpected\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "one username"):
                scraper.load_followers(path)

    def test_save_is_sorted_and_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "followers.csv"
            scraper.save_followers({"Bob", "alice", "bob"}, path)
            self.assertEqual(path.read_text(encoding="utf-8"), "alice\nbob\n")

    def test_failed_replace_preserves_existing_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "followers.csv"
            path.write_text("original\n", encoding="utf-8")

            with mock.patch("main.os.replace", side_effect=OSError("disk error")):
                with self.assertRaisesRegex(OSError, "disk error"):
                    scraper.save_followers({"replacement"}, path)

            self.assertEqual(path.read_text(encoding="utf-8"), "original\n")
            self.assertEqual(list(path.parent.glob(".followers.csv.*.tmp")), [])


class ParsingTests(unittest.TestCase):
    def test_extract_usernames_filters_non_profile_links(self):
        hrefs = [
            "/Alice/",
            "https://www.instagram.com/bob/?hl=en",
            "https://m.instagram.com/CHARLIE",
            "/alice/",
            "/accounts/login/",
            "/explore/",
            "/posts/not-a-profile/",
            "https://example.com/mallory/",
            "mailto:test@example.com",
            "relative/",
        ]
        self.assertEqual(
            scraper.extract_usernames(hrefs),
            {"alice", "bob", "charlie"},
        )

    def test_visible_username_links_exclude_avatar_and_unrelated_links(self):
        links = [
            ["/Alice/", "Alice"],
            ["/alice/", ""],
            ["/BOB/", "@bob"],
            ["/charlie/", "Charlie's profile"],
            ["/accounts/login/", "accounts"],
        ]
        self.assertEqual(
            scraper.extract_visible_username_links(links),
            {"alice", "bob"},
        )

    def test_parse_exact_follower_count(self):
        self.assertEqual(
            scraper.parse_expected_follower_count([None, "1,234 followers"]),
            1234,
        )
        self.assertEqual(
            scraper.parse_expected_follower_count(["1.234"]),
            1234,
        )
        self.assertIsNone(
            scraper.parse_expected_follower_count(["1.2K followers"])
        )

    def test_profile_url_must_match_the_monitored_username(self):
        self.assertTrue(
            scraper.profile_url_matches_username(
                "https://www.instagram.com/Target/", "target"
            )
        )
        self.assertFalse(
            scraper.profile_url_matches_username(
                "https://www.instagram.com/someone_else/", "target"
            )
        )
        self.assertFalse(
            scraper.profile_url_matches_username(
                "https://www.instagram.com/target/tagged/", "target"
            )
        )

    def test_comparison_is_set_based(self):
        unfollowed, new = scraper.compare_followers(
            {"alice", "bob"}, {"bob", "charlie"}
        )
        self.assertEqual(unfollowed, {"alice"})
        self.assertEqual(new, {"charlie"})


class ValidationTests(unittest.TestCase):
    def test_rejects_a_partial_count(self):
        result = scraper.ScrapeResult({"alice"}, 2, 10, True)
        with self.assertRaises(scraper.IncompleteScrapeError):
            scraper.validate_scrape_result(result)

    def test_accepts_more_verified_rows_than_a_stale_profile_counter(self):
        result = scraper.ScrapeResult({"alice", "bob"}, 1, 10, True)
        scraper.validate_scrape_result(result)

    def test_rejects_an_unverified_empty_result(self):
        result = scraper.ScrapeResult(set(), None, 10, True)
        with self.assertRaises(scraper.IncompleteScrapeError):
            scraper.validate_scrape_result(result)

    def test_accepts_a_verified_zero_follower_result(self):
        result = scraper.ScrapeResult(set(), 0, 3, True)
        scraper.validate_scrape_result(result)

    def test_any_scrape_without_a_profile_count_is_blocked_by_default(self):
        result = scraper.ScrapeResult({"alice"}, None, 10, True)
        with self.assertRaisesRegex(
            scraper.IncompleteScrapeError, "exact follower count"
        ):
            scraper.validate_scrape_result(result)

    def test_invalid_stability_setting_fails_before_browser_launch(self):
        with self.assertRaisesRegex(ValueError, "at least 1"):
            scraper.get_followers("owner", stable_bottom_rounds=0)


class FakeLinks:
    def __init__(self, dialog):
        self.dialog = dialog

    def evaluate_all(self, _expression):
        return list(self.dialog.hrefs)


class FakeDialog:
    def __init__(self, states, batches):
        self.states = states
        self.batches = batches
        self.index = 0
        self.hrefs = set()
        self.nudges = 0

    def locator(self, _selector):
        return FakeLinks(self)

    def evaluate(self, expression):
        if expression == scraper.COLLECT_DIALOG_USERNAMES_JAVASCRIPT:
            return [
                [href, href.strip("/")]
                for href in self.hrefs
            ]
        if expression == scraper.NUDGE_DIALOG_JAVASCRIPT:
            self.nudges += 1
            return True
        return self.states[self.index]

    def advance(self):
        self.hrefs.update(self.batches[self.index])
        if self.index < len(self.states) - 1:
            self.index += 1


class FakePage:
    def __init__(self, dialog):
        self.dialog = dialog

    def wait_for_timeout(self, _milliseconds):
        self.dialog.advance()


class ScrollTests(unittest.TestCase):
    def test_missing_scroller_cannot_prove_an_unknown_count_complete(self):
        no_scroller = {
            "found": False,
            "after": 0,
            "clientHeight": 300,
            "scrollHeight": 300,
            "atBottom": True,
        }
        dialog = FakeDialog(
            [no_scroller] * 4,
            [{"/alice/"}, set(), set(), set()],
        )

        with self.assertRaisesRegex(
            scraper.IncompleteScrapeError, "No scrollable follower list"
        ):
            scraper._scroll_to_end(
                FakePage(dialog),
                dialog,
                expected_count=None,
                wait_ms=0,
                stable_bottom_rounds=1,
                max_scroll_rounds=5,
                max_stalled_retries=0,
                timeout_seconds=10,
            )

    def test_scroll_waits_for_the_real_bottom_and_delayed_rows(self):
        middle_one = {
            "found": True,
            "after": 100,
            "clientHeight": 300,
            "scrollHeight": 900,
            "atBottom": False,
        }
        middle_two = {**middle_one, "after": 300}
        bottom = {
            "found": True,
            "after": 600,
            "clientHeight": 300,
            "scrollHeight": 900,
            "atBottom": True,
        }
        states = [middle_one, middle_two, bottom, bottom, bottom, bottom]
        batches = [
            {"/alice/"},
            set(),
            set(),
            {"/bob/"},
            set(),
            set(),
        ]
        dialog = FakeDialog(states, batches)

        followers, rounds = scraper._scroll_to_end(
            FakePage(dialog),
            dialog,
            expected_count=2,
            wait_ms=0,
            stable_bottom_rounds=2,
            max_scroll_rounds=10,
            max_stalled_retries=2,
            timeout_seconds=10,
        )

        self.assertEqual(followers, {"alice", "bob"})
        self.assertEqual(rounds, 6)

    def test_scroll_nudges_a_stalled_bottom_when_count_is_short(self):
        bottom = {
            "found": True,
            "after": 600,
            "clientHeight": 300,
            "scrollHeight": 900,
            "atBottom": True,
        }
        dialog = FakeDialog(
            [bottom] * 6,
            [{"/alice/"}, set(), {"/bob/"}, set(), set(), set()],
        )

        followers, _rounds = scraper._scroll_to_end(
            FakePage(dialog),
            dialog,
            expected_count=2,
            wait_ms=0,
            stable_bottom_rounds=1,
            max_scroll_rounds=10,
            max_stalled_retries=2,
            timeout_seconds=10,
        )

        self.assertEqual(followers, {"alice", "bob"})
        self.assertEqual(dialog.nudges, 1)

    def test_stall_retry_budget_resets_after_each_new_username(self):
        bottom = {
            "found": True,
            "after": 600,
            "clientHeight": 300,
            "scrollHeight": 900,
            "atBottom": True,
        }
        dialog = FakeDialog(
            [bottom] * 8,
            [
                {"/alice/"},
                set(),
                {"/bob/"},
                set(),
                set(),
                {"/charlie/"},
                set(),
                set(),
            ],
        )

        followers, _rounds = scraper._scroll_to_end(
            FakePage(dialog),
            dialog,
            expected_count=3,
            wait_ms=0,
            stable_bottom_rounds=1,
            max_scroll_rounds=10,
            max_stalled_retries=1,
            timeout_seconds=10,
        )

        self.assertEqual(followers, {"alice", "bob", "charlie"})
        self.assertEqual(dialog.nudges, 2)


class LocatorTests(unittest.TestCase):
    def test_followers_link_polling_handles_delayed_react_render(self):
        ticks = {"count": 0}
        page = mock.Mock()
        exact = mock.Mock()
        exact.first = exact
        exact.count.return_value = 0
        header_links = mock.Mock()
        header_links.count.side_effect = lambda: int(ticks["count"] >= 2)
        link = mock.Mock()
        link.inner_text.return_value = "1 follower"
        link.is_visible.return_value = True
        header_links.nth.return_value = link
        all_links = mock.Mock()
        all_links.count.return_value = 0

        def locate(selector):
            if selector == "header a[href]":
                return header_links
            if selector == "a[href]":
                return all_links
            return exact

        def advance(_milliseconds):
            ticks["count"] += 1

        page.locator.side_effect = locate
        page.wait_for_timeout.side_effect = advance

        self.assertIs(scraper._followers_link(page, "owner"), link)
        self.assertEqual(ticks["count"], 2)


class WorkflowTests(unittest.TestCase):
    def test_additions_only_refresh_the_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "followers.csv"
            path.write_text("alice\n", encoding="utf-8")
            result = scraper.ScrapeResult({"alice", "bob"}, 2, 8, True)

            environment = {
                "INSTAGRAM_USERNAME": "owner",
                "FOLLOWERS_CSV": str(path),
            }
            with mock.patch.dict(os.environ, environment, clear=True):
                with mock.patch("main.get_followers", return_value=result):
                    scraper.run()

            self.assertEqual(scraper.load_followers(path), {"alice", "bob"})

    def test_partial_scrape_never_overwrites_the_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "followers.csv"
            original = b"alice\nbob\n"
            path.write_bytes(original)
            result = scraper.ScrapeResult({"alice"}, 2, 8, True)

            environment = {
                "INSTAGRAM_USERNAME": "owner",
                "FOLLOWERS_CSV": str(path),
            }
            with mock.patch.dict(os.environ, environment, clear=True):
                with mock.patch("main.get_followers", return_value=result):
                    with self.assertRaises(scraper.IncompleteScrapeError):
                        scraper.run()

            self.assertEqual(path.read_bytes(), original)

    def test_failed_unfollow_notification_preserves_the_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "followers.csv"
            original = b"alice\nbob\n"
            path.write_bytes(original)
            result = scraper.ScrapeResult({"alice"}, 1, 8, True)

            environment = {
                "INSTAGRAM_USERNAME": "owner",
                "FOLLOWERS_CSV": str(path),
                "NTFY_TOPIC": "test-topic",
            }
            with mock.patch.dict(os.environ, environment, clear=True):
                with mock.patch("main.get_followers", return_value=result):
                    with mock.patch(
                        "main.send_notification",
                        side_effect=RuntimeError("ntfy unavailable"),
                    ):
                        with self.assertRaisesRegex(
                            RuntimeError, "ntfy unavailable"
                        ):
                            scraper.run()

            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
