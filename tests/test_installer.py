import unittest
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch, Mock

from modules.installer import (
    DEFAULT_PACKAGES,
    build_wheel_url,
    install_package,
    is_installed,
    is_compatible_installed_version,
    add_runtime_search_paths,
    start,
    is_compatible_wheel,
    runtime_packages,
    validate_python_version,
    wheel_tags,
)
from modules.progress import show_progress


class InstallerTests(unittest.TestCase):
    def test_installed_compatible_versions_are_reused_instead_of_exact_pins(self):
        for package, installed, fallback in [('numpy', '2.5.3', '2.1.3'),
                                               ('opencv-python', '4.12.0', '4.10.0.84')]:
            module = SimpleNamespace(__version__=installed)
            with patch('modules.installer.import_module', return_value=module), patch('modules.installer.logger'):
                detected, exists = is_installed(package, fallback)
            self.assertTrue(exists)
            self.assertIs(detected, module)
        for package, version in [('numpy', '1.26.4'), ('numpy', '3.0.0'),
                                  ('opencv-python', '4.9.0'), ('opencv-python', '5.0.0')]:
            self.assertFalse(is_compatible_installed_version(package, version))

    def test_complete_environment_never_tests_mirrors_or_downloads(self):
        numpy = SimpleNamespace(__version__='2.5.3')
        cv2 = SimpleNamespace(__version__='4.12.0')
        with patch('modules.installer.import_module', side_effect=lambda name: {'numpy': numpy, 'cv2': cv2}[name]), \
                patch('modules.installer.add_runtime_search_paths') as paths, \
                patch('modules.installer.test_mirrors') as mirrors, \
                patch('modules.installer.install_package') as install, patch('modules.installer.logger'):
            self.assertEqual(start(Mock()), [numpy, cv2])
        paths.assert_not_called()
        mirrors.assert_not_called()
        install.assert_not_called()

    def test_missing_opencv_download_does_not_redownload_existing_numpy(self):
        numpy = SimpleNamespace(__version__='2.5.3')
        cv2 = SimpleNamespace(__version__='4.10.0')
        def imports(name):
            if name == 'numpy':
                return numpy
            raise ImportError('cv2 missing')
        with patch('modules.installer.os', SimpleNamespace(name='nt', makedirs=Mock())), \
                patch('modules.installer.get_res_dir', return_value='fixture-packages'), \
                patch('modules.installer.add_runtime_search_paths'), \
                patch('modules.installer.import_module', side_effect=imports), \
                patch('modules.installer.test_mirrors', return_value=[('fixture', 'https://mirror.test')]) as mirrors, \
                patch('modules.installer.install_package', return_value=cv2) as install, \
                patch('modules.installer.logger'):
            self.assertEqual(start(Mock()), [numpy, cv2])
        mirrors.assert_called_once()
        install.assert_called_once()
        self.assertEqual(install.call_args.args[0], 'opencv-python')

    def test_compatible_cached_opencv_is_used_without_download(self):
        numpy = SimpleNamespace(__version__='2.5.3')
        cv2 = SimpleNamespace(__version__='4.10.0')
        calls = {'cv2': 0}
        def imports(name):
            if name == 'numpy':
                return numpy
            calls['cv2'] += 1
            if calls['cv2'] == 1:
                raise ImportError('not on search path yet')
            return cv2
        with patch('modules.installer.os', SimpleNamespace(name='nt', makedirs=Mock())), \
                patch('modules.installer.get_res_dir', return_value='fixture-packages'), \
                patch('modules.installer.add_runtime_search_paths') as paths, \
                patch('modules.installer.import_module', side_effect=imports), \
                patch('modules.installer.test_mirrors') as mirrors, \
                patch('modules.installer.install_package') as install, patch('modules.installer.logger'):
            self.assertEqual(start(Mock()), [numpy, cv2])
        paths.assert_called_once()
        mirrors.assert_not_called()
        install.assert_not_called()

    def test_incompatible_environment_is_not_silently_overwritten(self):
        with patch('modules.installer.import_module', return_value=SimpleNamespace(__version__='1.26.4')), \
                patch('modules.installer.test_mirrors') as mirrors, \
                patch('modules.installer.install_package') as install, patch('modules.installer.logger'):
            with self.assertRaisesRegex(RuntimeError, '不会自动覆盖'):
                start(Mock())
        mirrors.assert_not_called()
        install.assert_not_called()

    def test_runtime_paths_follow_virtual_environment_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'cv2').mkdir()
            (Path(directory) / 'numpy.libs').mkdir()
            with patch('modules.installer.sys.path', ['venv-site-packages']) as paths, \
                    patch('modules.installer.os.add_dll_directory', create=True):
                add_runtime_search_paths(directory)
                self.assertEqual(paths[0], 'venv-site-packages')
                self.assertIn(directory, paths[1:])

    def test_rejects_unsupported_python_version(self):
        for minor in (9, 14):
            with self.assertRaises(RuntimeError):
                validate_python_version(SimpleNamespace(major=3, minor=minor))

    def test_accepts_supported_python_versions(self):
        for minor in (10, 11, 12, 13):
            validate_python_version(SimpleNamespace(major=3, minor=minor))

    def test_runtime_packages_match_python_version(self):
        self.assertEqual(
            {"numpy": "2.1.3", "opencv-python": "4.10.0.84"},
            runtime_packages(SimpleNamespace(major=3, minor=13)),
        )
        for minor in (10, 11, 12):
            self.assertEqual(
                DEFAULT_PACKAGES,
                runtime_packages(SimpleNamespace(major=3, minor=minor)),
            )

    def test_parses_wheel_tags(self):
        self.assertEqual(
            wheel_tags("numpy-1.26.4-cp311-cp311-win_amd64.whl"),
            ("1.26.4", "cp311", "cp311", "win_amd64"),
        )

    def test_requires_matching_python_abi_and_platform(self):
        self.assertTrue(is_compatible_wheel(
            "numpy-1.26.4-cp311-cp311-win_amd64.whl",
            "numpy", "1.26.4", "cp311", "cp311", "win_amd64",
        ))
        self.assertFalse(is_compatible_wheel(
            "numpy-1.26.4-cp310-cp310-win_amd64.whl",
            "numpy", "1.26.4", "cp311", "cp311", "win_amd64",
        ))

    def test_accepts_opencv_abi3_wheel(self):
        self.assertTrue(is_compatible_wheel(
            "opencv_python-4.10.0.82-cp37-abi3-win_amd64.whl",
            "opencv-python", "4.10.0.82", "cp311", "cp311", "win_amd64",
        ))

    def test_opencv_wheel_requires_exact_build_version(self):
        self.assertFalse(is_compatible_wheel(
            'opencv_python-4.10.0.82-cp37-abi3-win_amd64.whl',
            'opencv-python', '4.10.0.84', 'cp313', 'cp313', 'win_amd64'))
        self.assertTrue(is_compatible_wheel(
            'opencv_python-4.10.0.84-cp37-abi3-win_amd64.whl',
            'opencv-python', '4.10.0.84', 'cp313', 'cp313', 'win_amd64'))

    def test_rejects_free_threaded_wheel(self):
        self.assertTrue(is_compatible_wheel(
            "numpy-2.1.3-cp313-cp313-win_amd64.whl",
            "numpy", "2.1.3", "cp313", "cp313", "win_amd64",
        ))
        self.assertFalse(is_compatible_wheel(
            "numpy-2.1.3-cp313-cp313t-win_amd64.whl",
            "numpy", "2.1.3", "cp313", "cp313", "win_amd64",
        ))

    def test_progress_handles_missing_content_length(self):
        output = StringIO()
        with redirect_stdout(output):
            show_progress("下载进度:", current=512, total=0)
        self.assertIn("已下载 512 bytes", output.getvalue())

    def test_resolves_relative_mirror_wheel_url(self):
        url = build_wheel_url(
            "https://mirrors.example/repository/pypi/simple/numpy/",
            "../../packages/abc/numpy-1.26.4-cp310-cp310-win_amd64.whl#sha256=test",
        )
        self.assertEqual(
            url,
            "https://mirrors.example/repository/pypi/packages/abc/numpy-1.26.4-cp310-cp310-win_amd64.whl",
        )

    @patch("modules.installer.import_module")
    @patch("modules.installer.extract_whl")
    @patch("modules.installer.clear_package_files")
    @patch("modules.installer.download_wheel")
    def test_falls_back_when_a_mirror_fails(
        self, download_wheel, clear_package_files, extract_whl, import_module
    ):
        download_wheel.side_effect = [
            RuntimeError("429 Client Error"),
            "numpy-1.26.4.whl",
        ]
        import_module.return_value = object()

        with patch("modules.installer.get_res_dir", return_value="packages"):
            result = install_package(
                "numpy", "1.26.4", [("华为", "https://huawei"), ("清华", "https://tsinghua")]
            )

        self.assertIsNotNone(result)
        self.assertEqual(download_wheel.call_count, 2)
        self.assertEqual(download_wheel.call_args_list[1].args[:3],
                         ("清华", "https://tsinghua", "numpy"))


if __name__ == "__main__":
    unittest.main()
