"""Tests for OnlyFans API error handling and retries.

OnlyFans' WAF sporadically answers valid requests with 400/401 ("Please refresh
the page"). The client must retry those and surface OnlyFans' own message
instead of the opaque "400 Client Error".
"""

import unittest

from unittest.mock import patch

import requests

from api.onlyfans_api import (
    RETRY_ATTEMPTS,
    OnlyFansApi,
    describe_api_error,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=''):
        self.status_code = status_code
        self.reason = 'Bad Request' if status_code >= 400 else 'OK'
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError('no json')

        return self._payload


class FakeSession:
    """Returns the given responses in order and counts requests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def request(self, **kwargs):
        self.calls += 1

        if self.responses:
            return self.responses.pop(0)

        return FakeResponse(200, {'isAuth': True})


def make_api(responses):
    api = OnlyFansApi(
        sess='dummy-session',
        auth_id='123456',
        auth_uid=None,
        user_agent='test-agent',
        x_bc='test-bc',
    )
    api.session = FakeSession(responses)

    # Do not hit the network for signing rules
    api.auth.get_headers = lambda url: {'sign': 'x', 'time': '1'}

    return api


WAF_ERROR = {'error': {'code': 401, 'message': 'Please refresh the page'}}
WAF_TEXT = '{"error":{"code":401,"message":"Please refresh the page"}}'


class DescribeApiErrorTests(unittest.TestCase):
    def test_reads_onlyfans_error_message(self):
        response = FakeResponse(400, WAF_ERROR, WAF_TEXT)

        self.assertEqual(describe_api_error(response), 'Please refresh the page')

    def test_falls_back_to_raw_text(self):
        response = FakeResponse(500, None, 'Internal Server Error')

        self.assertEqual(describe_api_error(response), 'Internal Server Error')

    def test_handles_empty_body(self):
        response = FakeResponse(500, None, '')

        self.assertEqual(describe_api_error(response), 'no details in the response')


class RetryTests(unittest.TestCase):
    def test_retries_a_transient_400_and_succeeds(self):
        api = make_api([FakeResponse(400, WAF_ERROR, WAF_TEXT), FakeResponse(200, {'isAuth': True})])

        with patch('api.onlyfans_api.time.sleep'):
            response = api._make_request('GET', '/users/me')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(api.session.calls, 2)

    def test_persistent_error_raises_with_onlyfans_message(self):
        api = make_api([FakeResponse(400, WAF_ERROR, WAF_TEXT) for _ in range(RETRY_ATTEMPTS)])

        with patch('api.onlyfans_api.time.sleep'):
            with self.assertRaises(requests.HTTPError) as context:
                api._make_request('GET', '/users/me')

        message = str(context.exception)
        self.assertIn('Please refresh the page', message)
        self.assertIn('400', message)
        self.assertEqual(context.exception.response.status_code, 400)
        self.assertEqual(api.session.calls, RETRY_ATTEMPTS)

    def test_post_requests_are_not_retried(self):
        api = make_api([FakeResponse(429, {'error': {'message': 'slow down'}})])

        with patch('api.onlyfans_api.time.sleep'):
            with self.assertRaises(requests.HTTPError):
                api._make_request('POST', '/users/otp/check')

        self.assertEqual(api.session.calls, 1)


if __name__ == '__main__':
    unittest.main()
