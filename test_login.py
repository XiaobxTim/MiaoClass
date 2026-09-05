import io
import unittest
from contextlib import redirect_stdout
from email.message import Message
from types import SimpleNamespace
from unittest.mock import patch

import requests

import main


CAS = 'https://cas.sustech.edu.cn/cas/login'
TIS = 'https://tis.sustech.edu.cn/cas?ticket=test-ticket'


class ScriptedAdapter(requests.adapters.BaseAdapter):
    """使用真实 Session 的跳转和 Cookie 处理，但不连接学校服务器。"""
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def send(self, request, **kwargs):
        self.calls.append(request)
        item = next(self.responses)
        if isinstance(item, Exception):
            raise item
        status, headers, body = item
        response = requests.Response()
        response.status_code = status
        response.url = request.url
        response.request = request
        response.headers.update(dict(headers))
        response._content = body.encode()
        message = Message()
        for name, value in headers:
            message.add_header(name, value)
        response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=message))
        return response

    def close(self):
        pass


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.saved_head = main.head.copy()
        self.addCleanup(lambda: (main.head.clear(), main.head.update(self.saved_head)))

    def run_login(self, tail, form=None):
        if form is None:
            form = "<input value='token&amp;value' type='hidden' name='execution'>"
        session = requests.Session()
        adapter = ScriptedAdapter([
            (200, [('Set-Cookie', 'JSESSIONID=cas-only; Path=/cas; Secure')], form),
            *tail,
        ])
        session.mount('https://', adapter)
        output = io.StringIO()
        with patch.object(main.requests, 'Session', return_value=session), redirect_stdout(output):
            result = main.cas_login('test-user', 'test-password')
        self.assertNotIn('test-password', output.getvalue())
        self.assertNotIn('test-ticket', output.getvalue())
        self.assertNotIn('cas-only', output.getvalue())
        self.assertNotIn('tis-session', output.getvalue())
        return result, adapter.calls, output.getvalue()

    def redirect(self):
        return (302, [('Location', TIS)], '')

    def test_missing_route_is_valid(self):
        result, calls, _ = self.run_login([
            self.redirect(),
            (200, [('Set-Cookie', 'JSESSIONID=tis-session; Path=/; Secure')], ''),
        ])
        self.assertEqual(result, ('', 'tis-session'))
        self.assertEqual(main.head['cookie'], 'JSESSIONID=tis-session')
        self.assertIn('execution=token%26value', calls[1].body)
        self.assertIn('JSESSIONID=cas-only', calls[1].headers['Cookie'])
        self.assertNotIn('Cookie', calls[2].headers)

    def test_session_cookie_after_tis_redirect(self):
        result, calls, _ = self.run_login([
            self.redirect(),
            (302, [('Location', '/home'),
                   ('Set-Cookie', 'SESSION=tis-session; Path=/; Secure; HttpOnly')], ''),
            (200, [], ''),
        ])
        self.assertEqual(result, ('', 'tis-session'))
        self.assertEqual(main.head['cookie'], 'SESSION=tis-session')
        self.assertEqual(calls[-1].headers['Cookie'], 'SESSION=tis-session')

    def test_empty_session_falls_back_to_jsessionid(self):
        result, _, _ = self.run_login([
            self.redirect(),
            (200, [('Set-Cookie', 'SESSION=; Path=/'),
                   ('Set-Cookie', 'JSESSIONID=tis-session; Path=/')], ''),
        ])
        self.assertEqual(result, ('', 'tis-session'))

    def test_cookies_across_redirects_and_multiple_headers(self):
        result, calls, _ = self.run_login([
            self.redirect(),
            (302, [('Location', '/home'), ('Set-Cookie', 'route=node1; Path=/'),
                   ('Set-Cookie', 'JSESSIONID=tis-session; Path=/')], ''),
            (200, [], ''),
        ])
        self.assertEqual(result, ('node1', 'tis-session'))
        self.assertIn('route=node1', calls[-1].headers['Cookie'])
        self.assertIn('JSESSIONID=tis-session', main.head['cookie'])

    def test_cas_cookie_does_not_count_as_tis_session(self):
        result, _, output = self.run_login([self.redirect(), (200, [], '')])
        self.assertEqual(result, ('', ''))
        self.assertIn('TIS未返回可用的会话Cookie', output)

    def test_redirect_back_to_cas_is_failure(self):
        result, _, output = self.run_login([
            self.redirect(),
            (302, [('Location', CAS), ('Set-Cookie', 'JSESSIONID=anonymous; Path=/')], ''),
            (200, [], 'login again'),
        ])
        self.assertEqual(result, ('', ''))
        self.assertIn('登录跳转未到达TIS', output)

    def test_missing_execution(self):
        result, calls, _ = self.run_login([], form='<html>maintenance</html>')
        self.assertEqual(result, ('', ''))
        self.assertEqual(len(calls), 1)

    def test_rejected_credentials(self):
        result, _, _ = self.run_login([(200, [], 'login form')])
        self.assertEqual(result, ('', ''))

    def test_server_error(self):
        result, _, output = self.run_login([self.redirect(), (503, [], '')])
        self.assertEqual(result, ('', ''))
        self.assertIn('503', output)

    def test_timeout_does_not_leak_ticket(self):
        result, _, output = self.run_login([
            self.redirect(), requests.Timeout('request failed: ' + TIS),
        ])
        self.assertEqual(result, ('', ''))
        self.assertIn('Timeout', output)

    def test_cas_401_reports_phase(self):
        result, _, output = self.run_login([(401, [], '')])
        self.assertEqual(result, ('', ''))
        self.assertIn('提交CAS登录表单失败', output)
        self.assertIn('HTTP 401', output)

    def test_tis_401_reports_phase_and_cookie_names(self):
        result, _, output = self.run_login([
            self.redirect(),
            (401, [('Set-Cookie', 'SESSION=secret-session; Path=/')], ''),
        ])
        self.assertEqual(result, ('', ''))
        self.assertIn('跟随登录跳转至TIS失败', output)
        self.assertIn('SESSION @ tis.sustech.edu.cn', output)
        self.assertNotIn('secret-session', output)

    def test_cookie_path_mismatch_visible_without_values(self):
        result, _, output = self.run_login([
            self.redirect(),
            (200, [('Set-Cookie', 'JSESSIONID=tis-session; Path=/cas')], ''),
        ])
        self.assertEqual(result, ('', ''))
        self.assertIn('JSESSIONID @ tis.sustech.edu.cn', output)
        self.assertIn('可发送给TIS接口的Cookie名称: 无', output)


if __name__ == '__main__':
    unittest.main()
