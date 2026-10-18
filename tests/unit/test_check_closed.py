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

"""Test the closed-issue checklist check."""

import json
from unittest import mock

import charmhub_listing_review.check_closed as check_closed

AUTOMATION = '<details>\n```\n## Listing requirements\n* [ ] Everything\n```\n</details>'
COMPLETE = '## Listing requirements\n* [x] One\n* [X] Two\n* [ ] The icon (recommended).'
INCOMPLETE = '## Listing requirements\n* [x] One\n* [ ] Two\n- [ ] Three'


def test_find_reviewer_checklist_skips_automation_comment():
    comments = [{'body': AUTOMATION}, {'body': 'chat'}, {'body': INCOMPLETE}, {'body': 'thanks'}]
    assert check_closed.find_reviewer_checklist(comments) == INCOMPLETE


def test_find_reviewer_checklist_none():
    assert check_closed.find_reviewer_checklist([{'body': AUTOMATION}]) is None


def test_unticked_items():
    assert check_closed.unticked_items(COMPLETE) == []
    assert check_closed.unticked_items(INCOMPLETE) == ['Two', 'Three']


def _run(comments: list[dict]):
    view = mock.Mock(stdout=json.dumps({'comments': comments}))
    with mock.patch('subprocess.run', return_value=view) as run:
        check_closed.check_closed_issue(7, repo='o/r')
    return [call.args[0] for call in run.call_args_list][1:]


def test_incomplete_reopens():
    calls = _run([{'body': INCOMPLETE}])
    assert calls[0][:4] == ['gh', 'issue', 'reopen', '7']
    assert '* Two' in calls[1][calls[1].index('--body') + 1]


def test_missing_checklist_reopens():
    assert _run([{'body': AUTOMATION}])[0][:3] == ['gh', 'issue', 'reopen']


def test_complete_labels_ready():
    calls = _run([{'body': COMPLETE}])
    assert calls[0][:3] == ['gh', 'issue', 'edit']
    assert check_closed.READY_LABEL in calls[0]
