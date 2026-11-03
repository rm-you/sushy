# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from unittest import mock

import requests

from sushy import connector
from sushy import exceptions
from sushy.tests.unit import base


class RetryAuthenticationLimitTestCase(base.TestCase):

    def _check_refresh_limit(self, status, method):
        conn = connector.Connector('https://bmc.example',
                                   server_side_retries=1)
        auth = mock.Mock()
        conn.set_auth(auth)
        unauthorized = requests.Response()
        unauthorized.status_code = 401
        unauthorized._content = b'{}'
        retry = requests.Response()
        retry.status_code = status
        retry._content = b'{"error": {"message": "SYS518 retry later"}}'
        responses = [unauthorized, retry, unauthorized]
        with mock.patch.object(conn._session, 'request', autospec=True,
                               side_effect=responses) as request, mock.patch(
                                   'time.sleep', autospec=True):
            self.assertRaises(
                exceptions.AccessError, conn._op, method, '/resource',
                headers={'Accept-Encoding': 'identity'})
        self.assertEqual(3, request.call_count)
        auth.refresh_session.assert_called_once_with()

    def test_server_error_after_refresh_does_not_allow_another_refresh(self):
        self._check_refresh_limit(500, 'GET')

    def test_bad_request_after_refresh_does_not_allow_another_refresh(self):
        self._check_refresh_limit(400, 'POST')

    def test_encoding_retry_after_refresh_does_not_allow_another_refresh(self):
        self._check_refresh_limit(406, 'GET')
