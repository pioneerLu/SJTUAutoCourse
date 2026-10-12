"""Windows 启动约束，不访问网页或读取实际配置。"""
import unittest
from unittest.mock import patch

from modules.runtime_env import ensure_windows, project_python


class PlatformSupportTests(unittest.TestCase):
    def test_supported_platform_accepts_startup(self):
        with patch('modules.runtime_env.os.name', 'nt'):
            ensure_windows()

    def test_unsupported_platform_stops_with_clear_message(self):
        with patch('modules.runtime_env.os.name', 'posix'):
            with self.assertRaisesRegex(SystemExit, 'Windows'):
                ensure_windows()

    def test_unsupported_platform_stops_before_environment_lookup(self):
        with patch('modules.runtime_env.os.name', 'posix'), patch('modules.runtime_env.Path') as path:
            with self.assertRaisesRegex(SystemExit, 'Windows'):
                project_python('unused')
            path.assert_not_called()


if __name__ == '__main__':
    unittest.main()
