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

"""Test the stale listing-request closer."""

import datetime
import json
from unittest import mock

import charmhub_listing_review.stale_reviews as stale_reviews

NOW = datetime.datetime(2026, 9, 13, tzinfo=datetime.UTC)
WARN_AFTER = datetime.timedelta(days=28)
CLOSE_AFTER = datetime.timedelta(days=2)


def _iso(dt: datetime.datetime) -> str:
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


@mock.patch('subprocess.run')
def test_list_open_listing_requests(mock_subprocess_run):
    issues = [{'number': 1, 'labels': [], 'updatedAt': _iso(NOW)}]
    mock_subprocess_run.return_value = mock.Mock(stdout=json.dumps(issues))
    result = stale_reviews.list_open_listing_requests(repo='canonical/example')
    assert result == issues
    mock_subprocess_run.assert_called_once_with(
        [
            'gh',
            'issue',
            'list',
            '--label',
            'listing-request',
            '--state',
            'open',
            '--json',
            'number,labels,updatedAt',
            '--limit',
            '200',
            '--repo',
            'canonical/example',
        ],
        capture_output=True,
        text=True,
        check=True,
    )


@mock.patch('subprocess.run')
def test_get_issue_comments(mock_subprocess_run):
    comments = [{'body': 'hello', 'createdAt': _iso(NOW)}]
    mock_subprocess_run.return_value = mock.Mock(stdout=json.dumps({'comments': comments}))
    result = stale_reviews.get_issue_comments(7, repo='canonical/example')
    assert result == comments
    mock_subprocess_run.assert_called_once_with(
        ['gh', 'issue', 'view', '7', '--json', 'comments', '--repo', 'canonical/example'],
        capture_output=True,
        text=True,
        check=True,
    )


@mock.patch('subprocess.run')
def test_warn_issue_posts_comment_and_label(mock_subprocess_run):
    stale_reviews.warn_issue(5, repo='canonical/example', dry_run=False)
    mock_subprocess_run.assert_any_call(
        [
            'gh',
            'issue',
            'comment',
            '5',
            '--body',
            stale_reviews.STALE_WARNING_COMMENT,
            '--repo',
            'canonical/example',
        ],
        check=True,
    )
    mock_subprocess_run.assert_any_call(
        ['gh', 'issue', 'edit', '5', '--add-label', 'stale', '--repo', 'canonical/example'],
        check=True,
    )


@mock.patch('subprocess.run')
def test_warn_issue_dry_run_does_not_call_gh(mock_subprocess_run):
    stale_reviews.warn_issue(5, repo=None, dry_run=True)
    mock_subprocess_run.assert_not_called()


@mock.patch('subprocess.run')
def test_close_stale_issue(mock_subprocess_run):
    stale_reviews.close_stale_issue(9, repo='canonical/example', dry_run=False)
    mock_subprocess_run.assert_called_once_with(
        [
            'gh',
            'issue',
            'close',
            '9',
            '--comment',
            stale_reviews.STALE_CLOSE_COMMENT,
            '--repo',
            'canonical/example',
        ],
        check=True,
    )


@mock.patch('charmhub_listing_review.stale_reviews.get_issue_comments')
def test_process_issue_active_recent_comment(mock_get_comments):
    mock_get_comments.return_value = [
        {'body': 'still working on it', 'createdAt': _iso(NOW - datetime.timedelta(days=1))}
    ]
    issue = {'number': 1, 'labels': [], 'updatedAt': _iso(NOW - datetime.timedelta(days=40))}
    action = stale_reviews.process_issue(issue, None, NOW, WARN_AFTER, CLOSE_AFTER, dry_run=True)
    assert action == 'active'


@mock.patch('charmhub_listing_review.stale_reviews.warn_issue')
@mock.patch('charmhub_listing_review.stale_reviews.get_issue_comments')
def test_process_issue_warns_after_inactivity(mock_get_comments, mock_warn_issue):
    mock_get_comments.return_value = [
        {'body': 'last comment', 'createdAt': _iso(NOW - datetime.timedelta(days=30))}
    ]
    issue = {'number': 2, 'labels': [], 'updatedAt': _iso(NOW - datetime.timedelta(days=30))}
    action = stale_reviews.process_issue(issue, None, NOW, WARN_AFTER, CLOSE_AFTER, dry_run=False)
    assert action == 'warned'
    mock_warn_issue.assert_called_once_with(2, None, False)


@mock.patch('charmhub_listing_review.stale_reviews.warn_issue')
@mock.patch('charmhub_listing_review.stale_reviews.get_issue_comments')
def test_process_issue_no_comments_uses_issue_updated_at(mock_get_comments, mock_warn_issue):
    mock_get_comments.return_value = []
    issue = {'number': 3, 'labels': [], 'updatedAt': _iso(NOW - datetime.timedelta(days=29))}
    action = stale_reviews.process_issue(issue, None, NOW, WARN_AFTER, CLOSE_AFTER, dry_run=False)
    assert action == 'warned'
    mock_warn_issue.assert_called_once_with(3, None, False)


@mock.patch('charmhub_listing_review.stale_reviews.close_stale_issue')
@mock.patch('charmhub_listing_review.stale_reviews.get_issue_comments')
def test_process_issue_closes_after_unanswered_warning(mock_get_comments, mock_close):
    mock_get_comments.return_value = [
        {
            'body': stale_reviews.STALE_WARNING_COMMENT,
            'createdAt': _iso(NOW - datetime.timedelta(days=3)),
        }
    ]
    issue = {'number': 4, 'labels': [{'name': 'stale'}], 'updatedAt': _iso(NOW)}
    action = stale_reviews.process_issue(issue, None, NOW, WARN_AFTER, CLOSE_AFTER, dry_run=False)
    assert action == 'closed'
    mock_close.assert_called_once_with(4, None, False)


@mock.patch('charmhub_listing_review.stale_reviews.close_stale_issue')
@mock.patch('charmhub_listing_review.stale_reviews.get_issue_comments')
def test_process_issue_waits_out_the_close_after_window(mock_get_comments, mock_close):
    mock_get_comments.return_value = [
        {
            'body': stale_reviews.STALE_WARNING_COMMENT,
            'createdAt': _iso(NOW - datetime.timedelta(hours=1)),
        }
    ]
    issue = {'number': 4, 'labels': [{'name': 'stale'}], 'updatedAt': _iso(NOW)}
    action = stale_reviews.process_issue(issue, None, NOW, WARN_AFTER, CLOSE_AFTER, dry_run=False)
    assert action == 'active'
    mock_close.assert_not_called()


@mock.patch('charmhub_listing_review.stale_reviews.unwarn_issue')
@mock.patch('charmhub_listing_review.stale_reviews.get_issue_comments')
def test_process_issue_reactivated_after_reply(mock_get_comments, mock_unwarn):
    mock_get_comments.return_value = [
        {
            'body': stale_reviews.STALE_WARNING_COMMENT,
            'createdAt': _iso(NOW - datetime.timedelta(days=3)),
        },
        {'body': "I'm still here", 'createdAt': _iso(NOW - datetime.timedelta(days=1))},
    ]
    issue = {'number': 4, 'labels': [{'name': 'stale'}], 'updatedAt': _iso(NOW)}
    action = stale_reviews.process_issue(issue, None, NOW, WARN_AFTER, CLOSE_AFTER, dry_run=False)
    assert action == 'reactivated'
    mock_unwarn.assert_called_once_with(4, None, False)


@mock.patch('charmhub_listing_review.stale_reviews.process_issue')
@mock.patch('charmhub_listing_review.stale_reviews.list_open_listing_requests')
def test_close_stale_requests_groups_by_action(mock_list_issues, mock_process_issue):
    mock_list_issues.return_value = [
        {'number': 1, 'labels': [], 'updatedAt': _iso(NOW)},
        {'number': 2, 'labels': [], 'updatedAt': _iso(NOW)},
    ]
    mock_process_issue.side_effect = ['warned', 'active']
    results = stale_reviews.close_stale_requests(repo='canonical/example')
    assert results == {'warned': [1], 'closed': [], 'reactivated': [], 'active': [2]}
