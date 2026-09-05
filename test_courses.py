import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import main


class CourseTests(unittest.TestCase):
    def response(self, payload):
        response = Mock(status_code=200)
        response.json.return_value = payload
        return response

    def run_download(self, responses, path):
        with patch.object(main, 'COURSE_INFO_PATH', str(path)), \
                patch.object(main, 'COURSE_TYPE', {'zynknjxk': '非培养方案内课程'}), \
                patch.object(main.requests, 'post', side_effect=responses) as post, \
                patch.object(main.time, 'sleep'), redirect_stdout(io.StringIO()):
            result = main.getinfo({'p_xn': '2026-2027', 'p_xq': '1', 'p_xnxq': '2026-20271'})
            return result, post.call_count

    def test_refresh_retry_and_pagination(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'course.txt'
            path.write_text('2026-20271\n{"old": ["old-id", "zynknjxk"]}')
            result, calls = self.run_download([
                self.response({'message': '访问频繁'}),
                self.response({'kxrwList': {'list': [{'rwmc': '目标课', 'id': 'new-id'}], 'pages': 2}}),
                self.response({'kxrwList': {'list': [{'rwmc': '第二门', 'id': 'second-id'}], 'pages': 2}}),
            ], path)
            self.assertEqual(calls, 3)
            self.assertEqual(set(result), {'目标课', '第二门'})
            self.assertEqual(set(json.loads(path.read_text().splitlines()[1])), set(result))

    def test_invalid_responses_do_not_save_partial_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'course.txt'
            with self.assertRaisesRegex(RuntimeError, '下载失败'):
                self.run_download([self.response({}) for _ in range(3)], path)
            self.assertFalse(path.exists())

    def test_explicit_empty_list_is_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _ = self.run_download(
                [self.response({'kxrwList': {'list': []}})], Path(directory) / 'course.txt')
            self.assertEqual(result, {})


if __name__ == '__main__':
    unittest.main()
