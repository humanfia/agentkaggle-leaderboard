from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from kagglesdk.competitions.types.submission_status import SubmissionStatus

from agentkaggle_leaderboard.kaggle_source import InvalidKaggleResponse
from agentkaggle_leaderboard.late_submissions import KaggleLateSubmissionSource


def competition(slug: str, deadline: datetime, *, title: str | None = None):
    return SimpleNamespace(
        ref=f"https://www.kaggle.com/competitions/{slug}",
        title=title or slug,
        deadline=deadline,
    )


def submission(
    submitted_at: datetime,
    *,
    team_name: str = "Alpha",
    status=SubmissionStatus.COMPLETE,
    public_score: str = "0.9",
    private_score: str = "0.8",
):
    return SimpleNamespace(
        date=submitted_at,
        team_name=team_name,
        status=status,
        public_score=public_score,
        private_score=private_score,
    )


class LateSubmissionSourceTests(unittest.TestCase):
    def test_entered_catalog_uses_page_numbers_and_deduplicates(self) -> None:
        calls: list[int] = []
        deadline = datetime(2026, 1, 1, tzinfo=timezone.utc)
        first_page = [competition(f"competition-{index}", deadline) for index in range(20)]

        class FakeApi:
            def competitions_list(self, **kwargs):
                calls.append(kwargs["page"])
                if kwargs["page"] == 1:
                    return SimpleNamespace(competitions=first_page)
                return SimpleNamespace(
                    competitions=[first_page[-1], competition("competition-20", deadline)]
                )

        source = KaggleLateSubmissionSource(
            FakeApi(), retry_attempts=1, min_request_interval_seconds=0.000001
        )
        competitions = source._list_entered_competitions()

        self.assertEqual(calls, [1, 2])
        self.assertEqual(len(competitions), 21)
        self.assertEqual(competitions[-1].title, "competition-20")

    def test_collect_filters_by_deadline_status_and_configured_team(self) -> None:
        deadline = datetime(2026, 6, 1, tzinfo=timezone.utc)
        pages = {
            "": SimpleNamespace(
                submissions=[
                    submission(datetime(2026, 6, 3, tzinfo=timezone.utc), team_name=" ＡLPHA "),
                    submission(
                        datetime(2026, 6, 2, tzinfo=timezone.utc),
                        status=SubmissionStatus.ERROR,
                    ),
                    submission(datetime(2026, 6, 2, tzinfo=timezone.utc), team_name="Other"),
                ],
                next_page_token="next",
            ),
            "next": SimpleNamespace(
                submissions=[
                    submission(deadline),
                    submission(datetime(2026, 5, 31, tzinfo=timezone.utc)),
                ],
                next_page_token="",
            ),
        }
        source = KaggleLateSubmissionSource(
            SimpleNamespace(), retry_attempts=1, min_request_interval_seconds=0.000001
        )
        source._list_entered_competitions = lambda: [
            competition("ended", deadline, title="Ended competition"),
            competition(
                "active",
                datetime(2026, 8, 1, tzinfo=timezone.utc),
                title="Active competition",
            ),
        ]
        page_calls: list[str] = []

        def list_page(slug: str, page_token: str):
            page_calls.append(page_token)
            return pages[page_token]

        source._list_submission_page = list_page
        scan = source.collect(
            {"alpha": "Alpha"},
            now=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        entries = scan.entries

        self.assertEqual(page_calls, ["", "next"])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].competition_title, "Ended competition")
        self.assertEqual(entries[0].configured_team_name, "Alpha")
        self.assertEqual(entries[0].public_score, "0.9")
        self.assertEqual(entries[0].private_score, "0.8")

    def test_submission_pagination_stops_after_reaching_deadline(self) -> None:
        deadline = datetime(2026, 6, 1, tzinfo=timezone.utc)
        source = KaggleLateSubmissionSource(
            SimpleNamespace(), retry_attempts=1, min_request_interval_seconds=0.000001
        )
        source._list_entered_competitions = lambda: [competition("ended", deadline)]
        calls: list[str] = []

        def list_page(slug: str, page_token: str):
            calls.append(page_token)
            return SimpleNamespace(
                submissions=[
                    submission(datetime(2026, 6, 2, tzinfo=timezone.utc)),
                    submission(datetime(2026, 5, 31, tzinfo=timezone.utc)),
                ],
                next_page_token="must-not-be-read",
            )

        source._list_submission_page = list_page
        scan = source.collect(
            {"alpha": "Alpha"},
            now=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        entries = scan.entries

        self.assertEqual(calls, [""])
        self.assertEqual(len(entries), 1)

    def test_auto_discovery_reads_the_latest_page_of_active_competitions(self) -> None:
        source = KaggleLateSubmissionSource(
            SimpleNamespace(), retry_attempts=1, min_request_interval_seconds=0.000001
        )
        source._list_entered_competitions = lambda: [
            competition(
                "active",
                datetime(2026, 8, 1, tzinfo=timezone.utc),
                title="Active competition",
            )
        ]
        calls: list[str] = []

        def list_page(slug: str, page_token: str):
            calls.append(page_token)
            return SimpleNamespace(
                submissions=[
                    submission(
                        datetime(2026, 7, 1, tzinfo=timezone.utc),
                        team_name=" Dynamic Team ",
                    )
                ],
                next_page_token="must-not-be-read",
            )

        source._list_submission_page = list_page
        scan = source.collect(
            {},
            now=datetime(2026, 7, 2, tzinfo=timezone.utc),
            discover_teams=True,
        )

        self.assertEqual(calls, [""])
        self.assertEqual(scan.entries, ())
        self.assertEqual(scan.discovered_team_names, ("Dynamic Team",))

    def test_auto_discovery_includes_unconfigured_late_submissions(self) -> None:
        deadline = datetime(2026, 6, 1, tzinfo=timezone.utc)
        source = KaggleLateSubmissionSource(
            SimpleNamespace(), retry_attempts=1, min_request_interval_seconds=0.000001
        )
        source._list_entered_competitions = lambda: [
            competition("ended", deadline, title="Ended competition")
        ]
        source._list_submission_page = lambda slug, page_token: SimpleNamespace(
            submissions=[
                submission(
                    datetime(2026, 6, 2, tzinfo=timezone.utc),
                    team_name="Discovered Team",
                )
            ],
            next_page_token="",
        )

        scan = source.collect(
            {},
            now=datetime(2026, 7, 1, tzinfo=timezone.utc),
            discover_teams=True,
        )

        self.assertEqual(scan.discovered_team_names, ("Discovered Team",))
        self.assertEqual(len(scan.entries), 1)
        self.assertEqual(scan.entries[0].configured_team_name, "Discovered Team")

    def test_repeated_submission_page_token_is_rejected(self) -> None:
        deadline = datetime(2026, 6, 1, tzinfo=timezone.utc)
        source = KaggleLateSubmissionSource(
            SimpleNamespace(), retry_attempts=1, min_request_interval_seconds=0.000001
        )
        source._list_entered_competitions = lambda: [competition("ended", deadline)]
        source._list_submission_page = lambda slug, page_token: SimpleNamespace(
            submissions=[submission(datetime(2026, 6, 2, tzinfo=timezone.utc))],
            next_page_token="repeated",
        )

        with self.assertRaises(InvalidKaggleResponse):
            source.collect(
                {"alpha": "Alpha"},
                now=datetime(2026, 7, 1, tzinfo=timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
