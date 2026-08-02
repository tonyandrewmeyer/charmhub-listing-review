# Copyright 2026 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for the reminder poster."""

import datetime
import json
from unittest import mock

import pytest

import charmhub_listing_review.remind as remind


def _issue(
    number: int = 42,
    *,
    assignees: list[str] | None = None,
    created_at: str = '2026-01-01T00:00:00Z',
    updated_at: str | None = None,
) -> remind._Issue:
    return {
        'number': number,
        'title': 'Review foo',
        'assignees': [{'login': login} for login in (assignees or ['alice'])],
        'createdAt': created_at,
        'updatedAt': updated_at or created_at,
    }


def _comment(body: str, created_at: str, author: str = 'someone') -> remind._IssueComment:
    return {
        'author': {'login': author},
        'body': body,
        'createdAt': created_at,
    }


def test_is_bot_reminder_flags_marker():
    assert remind._is_bot_reminder(_comment(remind.REMINDER_MARKER + '\nHi', 'x'))
    assert not remind._is_bot_reminder(_comment('regular comment', 'x'))


def test_days_since_last_human_activity_uses_created_when_no_comments():
    now = datetime.datetime(2026, 1, 11, tzinfo=datetime.UTC)
    issue = _issue(created_at='2026-01-01T00:00:00Z')
    days = remind.days_since_last_human_activity(issue, comments=[], now=now)
    assert days == pytest.approx(10.0)


def test_days_since_last_human_activity_ignores_bot_reminders():
    now = datetime.datetime(2026, 1, 15, tzinfo=datetime.UTC)
    issue = _issue(created_at='2026-01-01T00:00:00Z')
    comments = [
        _comment('a real update', '2026-01-05T00:00:00Z'),
        _comment(remind.REMINDER_MARKER + '\nbot ping', '2026-01-14T00:00:00Z'),
    ]
    # 15 - 5 = 10 (the bot reminder does not reset the clock).
    days = remind.days_since_last_human_activity(issue, comments=comments, now=now)
    assert days == pytest.approx(10.0)


def test_days_since_last_human_activity_picks_latest_human_comment():
    now = datetime.datetime(2026, 1, 20, tzinfo=datetime.UTC)
    issue = _issue(created_at='2026-01-01T00:00:00Z')
    comments = [
        _comment('first', '2026-01-05T00:00:00Z'),
        _comment('second', '2026-01-15T00:00:00Z'),
    ]
    days = remind.days_since_last_human_activity(issue, comments=comments, now=now)
    assert days == pytest.approx(5.0)


def test_hours_since_last_reminder_returns_none_when_no_reminder():
    now = datetime.datetime(2026, 1, 15, tzinfo=datetime.UTC)
    comments = [_comment('hello', '2026-01-10T00:00:00Z')]
    assert remind.hours_since_last_reminder(comments, now=now) is None


def test_hours_since_last_reminder_picks_latest_marker():
    now = datetime.datetime(2026, 1, 15, tzinfo=datetime.UTC)
    comments = [
        _comment(remind.REMINDER_MARKER + '\nold', '2026-01-10T00:00:00Z'),
        _comment('normal', '2026-01-11T00:00:00Z'),
        _comment(remind.REMINDER_MARKER + '\nnew', '2026-01-14T00:00:00Z'),
    ]
    # 24h between 2026-01-14 and 2026-01-15.
    assert remind.hours_since_last_reminder(comments, now=now) == pytest.approx(24.0)


def test_format_reminder_normal_pings_assignees():
    body = remind.format_reminder(
        [{'login': 'alice'}, {'login': 'bob'}],
        idle_days=8.4,
        escalate=False,
    )
    assert body.startswith(remind.REMINDER_MARKER)
    assert '@alice' in body and '@bob' in body
    assert 'no activity for 8 days' in body
    assert remind.ESCALATION_TEAM not in body


def test_format_reminder_escalation_pings_team():
    body = remind.format_reminder(
        [{'login': 'alice'}],
        idle_days=15.0,
        escalate=True,
    )
    assert remind.ESCALATION_TEAM in body
    assert '@alice' in body


def test_format_reminder_no_assignee_fallback():
    body = remind.format_reminder([], idle_days=8.0, escalate=False)
    assert 'no assignee' in body.lower()


def test_decide_and_post_skips_when_idle_below_threshold():
    now = datetime.datetime(2026, 1, 5, tzinfo=datetime.UTC)
    issue = _issue(created_at='2026-01-01T00:00:00Z')
    with mock.patch.object(remind, 'post_comment') as post:
        status = remind.decide_and_post(
            issue,
            comments=[],
            now=now,
            remind_after_days=7,
            escalate_after_days=14,
            min_hours_between_reminders=20.0,
            dry_run=False,
            repo=None,
        )
    post.assert_not_called()
    assert status.startswith('skipped')


def test_decide_and_post_reminds_and_posts():
    now = datetime.datetime(2026, 1, 10, tzinfo=datetime.UTC)
    issue = _issue(created_at='2026-01-01T00:00:00Z', assignees=['alice'])
    with mock.patch.object(remind, 'post_comment') as post:
        status = remind.decide_and_post(
            issue,
            comments=[],
            now=now,
            remind_after_days=7,
            escalate_after_days=14,
            min_hours_between_reminders=20.0,
            dry_run=False,
            repo='canonical/charmhub-listing-review',
        )
    post.assert_called_once()
    args, kwargs = post.call_args
    assert args[0] == 42
    assert '@alice' in args[1]
    assert remind.ESCALATION_TEAM not in args[1]
    assert kwargs['repo'] == 'canonical/charmhub-listing-review'
    assert status.startswith('reminded')


def test_decide_and_post_escalates_after_second_threshold():
    now = datetime.datetime(2026, 1, 20, tzinfo=datetime.UTC)
    issue = _issue(created_at='2026-01-01T00:00:00Z', assignees=['alice'])
    with mock.patch.object(remind, 'post_comment') as post:
        status = remind.decide_and_post(
            issue,
            comments=[],
            now=now,
            remind_after_days=7,
            escalate_after_days=14,
            min_hours_between_reminders=20.0,
            dry_run=False,
            repo=None,
        )
    post.assert_called_once()
    body = post.call_args[0][1]
    assert remind.ESCALATION_TEAM in body
    assert status.startswith('escalated')


def test_decide_and_post_suppresses_if_recent_reminder():
    now = datetime.datetime(2026, 1, 10, tzinfo=datetime.UTC)
    issue = _issue(created_at='2026-01-01T00:00:00Z')
    comments = [
        _comment(
            remind.REMINDER_MARKER + '\nrecent reminder',
            '2026-01-09T12:00:00Z',
        ),
    ]
    with mock.patch.object(remind, 'post_comment') as post:
        status = remind.decide_and_post(
            issue,
            comments=comments,
            now=now,
            remind_after_days=7,
            escalate_after_days=14,
            min_hours_between_reminders=20.0,
            dry_run=False,
            repo=None,
        )
    post.assert_not_called()
    assert status.startswith('skipped')


def test_decide_and_post_dry_run_does_not_post(capsys):
    now = datetime.datetime(2026, 1, 10, tzinfo=datetime.UTC)
    issue = _issue(created_at='2026-01-01T00:00:00Z')
    with mock.patch.object(remind, 'post_comment') as post:
        status = remind.decide_and_post(
            issue,
            comments=[],
            now=now,
            remind_after_days=7,
            escalate_after_days=14,
            min_hours_between_reminders=20.0,
            dry_run=True,
            repo=None,
        )
    post.assert_not_called()
    assert 'dry run' in status
    captured = capsys.readouterr()
    assert remind.REMINDER_MARKER in captured.out


def test_list_open_listing_requests_invokes_gh():
    stub_stdout = json.dumps([
        {
            'number': 1,
            'title': 't',
            'assignees': [{'login': 'x'}],
            'createdAt': '2026-01-01T00:00:00Z',
            'updatedAt': '2026-01-01T00:00:00Z',
        }
    ])
    with mock.patch('subprocess.run') as run:
        run.return_value = mock.Mock(stdout=stub_stdout)
        issues = remind.list_open_listing_requests(repo='canonical/x')
    assert len(issues) == 1
    cmd = run.call_args[0][0]
    assert cmd[:2] == ['gh', 'issue']
    assert '--repo' in cmd and 'canonical/x' in cmd
    assert '--label' in cmd and 'listing-request' in cmd


def test_main_reports_no_issues_and_exits_zero(capsys):
    with mock.patch.object(remind, 'list_open_listing_requests', return_value=[]):
        rc = remind.main(['--dry-run'])
    assert rc == 0
    assert 'No open listing-request issues' in capsys.readouterr().out


def test_main_rejects_bad_thresholds():
    with pytest.raises(SystemExit):
        remind.main(['--remind-after', '10', '--escalate-after', '5'])
