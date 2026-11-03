# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from email.parser import BytesParser
from email import policy
import io
import json
import tempfile
from unittest import mock

import requests

from sushy import connector
from sushy import exceptions
from sushy.resources import constants
from sushy.resources.updateservice import updateservice
from sushy import taskmonitor
from sushy.tests.unit import base


class MultipartTestCase(base.TestCase):

    def setUp(self):
        super().setUp()
        self.conn = connector.Connector('https://bmc.example')
        self.response = requests.Response()
        self.response.status_code = 202
        self.response.headers['Location'] = '/TaskMonitor/1'
        self.response._content = b''

    def _chunks(self, body):
        if hasattr(body, 'read'):
            yield from iter(lambda: body.read(8192), b'')
        else:
            yield from body

    def _consume(self, body):
        return b''.join(self._chunks(body))

    def test_streaming_wire_format(self):
        image = b'firmware' * 100000
        with tempfile.TemporaryFile() as stream:
            stream.write(image)
            stream.seek(0)
            fields = {
                'UpdateParameters': (None, '{"Targets": ["/inventory/BMC"]}',
                                     'application/json'),
                'OemParameters': (None, '{"ImageType": "BMC"}',
                                  'application/json'),
                'UpdateFile': ('fw.bin', stream, 'application/octet-stream'),
            }

            def request(method, url, **kwargs):
                self.assertNotIn('json', kwargs)
                self.assertFalse(kwargs['allow_redirects'])
                prepared = requests.Request(
                    method, url, data=kwargs['data'],
                    headers=kwargs['headers']).prepare()
                wire = self._consume(prepared.body)
                self.assertEqual(len(wire),
                                 int(prepared.headers['Content-Length']))
                self.assertNotIn('Transfer-Encoding', prepared.headers)
                message = BytesParser(policy=policy.default).parsebytes(
                    ('Content-Type: ' + prepared.headers['Content-Type']
                     + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + wire)
                parts = list(message.iter_parts())
                self.assertEqual(3, len(parts))
                self.assertEqual('application/json',
                                 parts[0].get_content_type())
                self.assertEqual('application/json',
                                 parts[1].get_content_type())
                self.assertEqual('application/octet-stream',
                                 parts[2].get_content_type())
                self.assertEqual({'Targets': ['/inventory/BMC']},
                                 json.loads(parts[0].get_payload(decode=True)))
                self.assertEqual({'ImageType': 'BMC'},
                                 json.loads(parts[1].get_payload(decode=True)))
                self.assertEqual(image, parts[2].get_payload(decode=True))
                return self.response

            with mock.patch.object(self.conn._session, 'request',
                                   autospec=True, side_effect=request):
                self.conn.post_multipart('/upload', fields)
            self.assertFalse(stream.closed)

    def _test_retry(self, failure):
        bodies = []
        image = io.BytesIO(b'firmware bytes')

        def request(method, url, **kwargs):
            bodies.append(self._consume(kwargs['data']))
            if len(bodies) == 1:
                if isinstance(failure, Exception):
                    raise failure
                return failure
            return self.response

        with mock.patch.object(self.conn._session, 'request', autospec=True,
                               side_effect=request), mock.patch(
                                   'time.sleep', autospec=True):
            self.conn.post_multipart('/upload', {
                'UpdateParameters': (None, '{}', 'application/json'),
                'UpdateFile': ('fw.bin', image, 'application/octet-stream')})
        self.assertEqual(2, len(bodies))
        self.assertEqual(bodies[0], bodies[1])
        self.assertIn(b'firmware bytes', bodies[1])

    def test_connect_timeout_replays_whole_image(self):
        self._test_retry(requests.exceptions.ConnectTimeout('no connection'))

    def test_error_after_connecting_not_sent_again(self):
        # The image may have reached the BMC, which may be applying it.
        for failure in (requests.exceptions.ConnectionError('mid-upload'),
                        requests.exceptions.ReadTimeout('no answer'),
                        requests.exceptions.ChunkedEncodingError('cut')):
            with self.subTest(failure=failure):
                with mock.patch.object(self.conn._session, 'request',
                                       autospec=True,
                                       side_effect=failure) as request, \
                        mock.patch('time.sleep', autospec=True) as sleep:
                    error = self.assertRaises(
                        exceptions.ConnectionError, self.conn.post_multipart,
                        '/upload', {'UpdateFile': (
                            'fw.bin', io.BytesIO(b'firmware bytes'),
                            'application/octet-stream')})
                request.assert_called_once()
                sleep.assert_not_called()
                self.assertIn('so it is not sent again', str(error))

    def test_reauthentication_replays_whole_image(self):
        auth = mock.Mock()
        self.conn.set_auth(auth)
        response = requests.Response()
        response.status_code = 401
        response._content = b'{}'
        self._test_retry(response)
        auth.refresh_session.assert_called_once_with()

    def test_server_retry_replays_whole_image(self):
        self.conn._server_side_retries = 1
        response = requests.Response()
        response.status_code = 503
        response._content = b'{"error": {"message": "SYS518 retry later"}}'
        self._test_retry(response)


class MultipartUpdateServiceTestCase(base.TestCase):

    def setUp(self):
        super().setUp()
        self.conn = mock.Mock()
        sample = 'sushy/tests/unit/json_samples/updateservice.json'
        with open(sample) as stream:
            self.sample = json.load(stream)
        self.conn.get.return_value.json.return_value = self.sample
        self.service = updateservice.UpdateService(
            self.conn, '/redfish/v1/UpdateService')
        response = requests.Response()
        response.status_code = 202
        response.headers['Location'] = '/TaskMonitor/1'
        response._content = b''
        self.conn.post_multipart.return_value = response

    def test_parameters_and_task_monitor(self):
        stream = io.BytesIO(b'firmware')
        monitor = self.service.multipart_update(
            stream, targets=['/inventory/BMCImage1'],
            oem_parameters={'ImageType': 'BMC'},
            apply_time=constants.ApplyTime.IMMEDIATE, force_update=True)
        self.assertIsInstance(monitor, taskmonitor.TaskMonitor)
        self.assertEqual('/TaskMonitor/1', monitor.task_monitor_uri)
        self.assertFalse(stream.closed)
        call = self.conn.post_multipart.call_args
        self.assertEqual('/redfish/v1/UpdateService/upload', call.args[0])
        fields = call.kwargs['fields']
        self.assertEqual({'Targets': ['/inventory/BMCImage1'],
                          '@Redfish.OperationApplyTime': 'Immediate',
                          'ForceUpdate': True},
                         json.loads(fields['UpdateParameters'][1]))
        self.assertEqual({'ImageType': 'BMC'},
                         json.loads(fields['OemParameters'][1]))
        self.assertIs(stream, fields['UpdateFile'][1])

    def test_path_and_optional_parameters(self):
        with tempfile.NamedTemporaryFile() as stream:
            stream.write(b'firmware')
            stream.flush()
            self.service.multipart_update(stream.name)
        fields = self.conn.post_multipart.call_args.kwargs['fields']
        self.assertEqual({}, json.loads(fields['UpdateParameters'][1]))
        self.assertNotIn('OemParameters', fields)
        self.assertTrue(fields['UpdateFile'][1].closed)

    def test_missing_uri(self):
        self.sample.pop('MultipartHttpPushUri')
        self.service._parse_attributes(self.sample)
        self.assertRaisesRegex(exceptions.MissingAttributeError,
                               'MultipartHttpPushUri',
                               self.service.multipart_update, '/no/image')
        self.conn.post_multipart.assert_not_called()

    def test_oversized_image(self):
        self.sample['MaxImageSizeBytes'] = 2
        self.service._parse_attributes(self.sample)
        self.assertRaises(exceptions.InvalidParameterValueError,
                          self.service.multipart_update, io.BytesIO(b'large'))
        self.conn.post_multipart.assert_not_called()

    def test_push_without_actions(self):
        self.sample.pop('Actions')
        self.service._parse_attributes(self.sample)
        self.service.multipart_update(io.BytesIO(b'firmware'))
        self.conn.post_multipart.assert_called_once()
