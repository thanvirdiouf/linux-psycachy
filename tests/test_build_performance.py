# SPDX-License-Identifier: GPL-2.0-only
"""Test flag preservation, real ThinLTO linking and measurement exit handling."""

import csv
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
LINKER = ROOT / 'scripts/psycachy-ld'
MEASURE = ROOT / 'scripts/measure_build.py'


class LinkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='psycachy-link-')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.capture = self.folder / 'arguments.json'
        fake = self.folder / 'fake linker'
        fake.write_text(f'#!{sys.executable}\nimport json, os, sys\n'
                        'open(os.environ["CAPTURE"], "w").write(json.dumps(sys.argv[1:]))\n')
        fake.chmod(0o755)
        self.env = dict(os.environ, PSYCACHY_REAL_LD=str(fake), CAPTURE=str(self.capture),
                        PSYCACHY_THINLTO_TUNING='1', PSYCACHY_MODULE_LINK_JOBS='1',
                        PSYCACHY_KERNEL_LINK_JOBS='4', PSYCACHY_THINLTO_CACHE=str(self.folder / 'cache with spaces'))

    def invoke(self, *args, **env):
        subprocess.run([str(LINKER), *args], env=dict(self.env, **env), check=True)
        return json.loads(self.capture.read_text())

    def test_module_links_preserve_kbuild_flags_and_argument_boundaries(self):
        args = ['-r', '--lto-whole-program-visibility', '-mllvm', '-import-instr-limit=5',
                '--build-id=sha1', '-T', 'module script.lds', '-o', 'drivers/test.ko', '@objects']
        result = self.invoke(*args)
        self.assertEqual(result[:len(args)], args)
        self.assertIn('--threads=1', result)
        self.assertIn('--thinlto-jobs=1', result)
        self.assertIn(f'--thinlto-cache-dir={self.folder / "cache with spaces"}', result)

    def test_core_links_use_kernel_budget(self):
        for output in ('vmlinux.o', '.tmp_vmlinux1', 'vmlinux'):
            with self.subTest(output=output):
                result = self.invoke('-r', f'--output={output}', 'input.o')
                self.assertIn('--thinlto-jobs=4', result)
                self.assertIn('--threads=4', result)

    def test_linker_probes_are_untouched(self):
        for args in (['--version'], ['-v'], ['-r', '-o', '/dev/null', 'input.o']):
            self.assertEqual(self.invoke(*args), args)

    def test_baseline_can_disable_thread_tuning_and_cache_independently(self):
        args = ['-r', '-otest.o', 'input.o']
        result = self.invoke(*args, PSYCACHY_THINLTO_TUNING='0', PSYCACHY_THINLTO_CACHE='')
        self.assertEqual(result, args)
        result = self.invoke(*args, PSYCACHY_THINLTO_TUNING='0')
        self.assertFalse(any(arg.startswith('--thinlto-jobs') for arg in result))
        self.assertTrue(any(arg.startswith('--thinlto-cache-dir') for arg in result))

    @unittest.skipUnless(shutil.which('clang-18') and shutil.which('ld.lld-18'), 'LLVM 18 not installed')
    def test_real_thinlto_link_and_cache_reuse(self):
        source = self.folder / 'input.c'
        source.write_text('int exported_function(int value) { return value + 7; }\n')
        obj = self.folder / 'input.o'
        subprocess.run(['clang-18', '-O3', '-flto=thin', '-c', str(source), '-o', str(obj)], check=True)
        cache = self.folder / 'cache with spaces'
        cache.mkdir()
        env = dict(self.env, PSYCACHY_REAL_LD=shutil.which('ld.lld-18'))
        output = self.folder / 'test.ko'
        args = [str(LINKER), '-r', '--lto-whole-program-visibility', '-mllvm',
                '-import-instr-limit=5', '--build-id=sha1', '-o', str(output), str(obj)]
        subprocess.run(args, env=env, check=True)
        entries = {path.name: path.read_bytes() for path in cache.glob('llvmcache-*')}
        self.assertTrue(entries, 'ThinLTO must create native object cache entries')
        first = output.read_bytes()
        subprocess.run(args, env=env, check=True)
        self.assertEqual(output.read_bytes(), first)
        self.assertEqual({path.name: path.read_bytes() for path in cache.glob('llvmcache-*')}, entries)
        source.write_text('int exported_function(int value) { return value + 9; }\n')
        subprocess.run(['clang-18', '-O3', '-flto=thin', '-c', str(source), '-o', str(obj)], check=True)
        subprocess.run(args, env=env, check=True)
        self.assertNotEqual(output.read_bytes(), first, 'Changed code must not reuse stale cached output')


class MeasurementsTests(unittest.TestCase):
    def test_termination_stops_build_and_writes_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            marker = folder / 'started'
            process = subprocess.Popen([sys.executable, str(MEASURE), '--output', temporary,
                                        '--interval', '0.02', '--', sys.executable, '-c',
                                        f'from pathlib import Path; import time; Path({str(marker)!r}).touch(); time.sleep(30)'],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.exists(), 'Measured child should start')
                process.terminate()
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 143, stdout + stderr)
                self.assertEqual(json.loads((folder / 'summary.json').read_text())['exit_status'], 143)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_records_resources_and_preserves_command_exit_status(self):
        for status in (0, 7):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                result = subprocess.run([sys.executable, str(MEASURE), '--output', temp,
                                         '--interval', '0.02', '--', sys.executable, '-c',
                                         f'import time; time.sleep(0.06); raise SystemExit({status})'],
                                        text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, status, result.stderr)
                summary = json.loads((Path(temp) / 'summary.json').read_text())
                self.assertEqual(summary['exit_status'], status)
                self.assertGreater(summary['elapsed_seconds'], 0)
                self.assertGreater(summary['min_sampled_available_memory_bytes'], 0)
                with (Path(temp) / 'resources.csv').open() as stream:
                    rows = list(csv.DictReader(stream))
                self.assertGreater(len(rows), 2)
                self.assertTrue((Path(temp) / 'time.txt').stat().st_size)


if __name__ == '__main__':
    unittest.main()
