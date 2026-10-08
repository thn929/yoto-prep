import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import yoto_prep as prep
import yoto_review as review


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg required')
class PhoneReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        patcher = patch.object(prep, 'ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.folder = self.root / 'jobs' / 'abcdefghijk'
        self.folder.mkdir(parents=True)
        self.source = self.folder / 'source.wav'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=3', str(self.source)], check=True)
        self.original = self.source.read_bytes()
        self.job = {'id': 'abcdefghijk', 'title': 'A book / with punctuation', 'status': 'ready',
                    'source': str(self.source), 'duration': 3, 'keep': {'start': .2, 'end': 2.8}}
        prep.save(self.folder / 'job.json', self.job)
        (self.folder / 'report.txt').write_text('Original report\n')
        self.server = review.ReviewServer(('127.0.0.1', 0), 'test-token')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, body=None, token='test-token'):
        req = Request(self.base + path, headers={'X-Review-Token': token},
                      data=json.dumps(body).encode() if body is not None else None)
        return urlopen(req, timeout=10)

    def test_access_and_job_listing(self):
        with self.assertRaises(HTTPError) as error:
            self.request('/api/jobs', token='wrong')
        self.assertEqual(error.exception.code, 403)
        with self.request('/api/jobs') as response:
            self.assertTrue(json.load(response)[0]['ready'])
        with self.request('/api/job?id=abcdefghijk') as response:
            self.assertEqual(json.load(response)['keep'], self.job['keep'])
        with self.request('/') as response:
            self.assertIn(b'Crop & download', response.read())
        with self.assertRaises(HTTPError):
            self.request('/api/job?id=../../secret')
        # A token in the URL alone cannot authorize a cross-site form submission.
        with self.assertRaises(HTTPError) as error:
            self.request('/api/crop?token=test-token', {'id': 'abcdefghijk', 'start': 0, 'end': 2}, token='')
        self.assertEqual(error.exception.code, 403)

    def test_real_previews_leave_job_unchanged(self):
        for edge in ('intro', 'outro'):
            with self.request('/api/preview?id=abcdefghijk&start=0.413&end=2.217&edge=' + edge) as response:
                self.assertEqual(response.headers['Content-Type'], 'audio/mp4')
                path = self.root / f'{edge}.m4a'
                path.write_bytes(response.read())
            duration = float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
                'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(path)], text=True))
            self.assertAlmostEqual(duration, 1.804, delta=.03)
        self.assertEqual(prep.read(self.folder / 'job.json'), self.job)
        self.assertEqual(list(self.folder.glob('.preview-*')), [])

    def test_crop_download_versions_and_cli_resume(self):
        paths = []
        for start in (.3, .5):
            with self.request('/api/crop', {'id': 'abcdefghijk', 'start': start, 'end': 2.7}) as response:
                result = json.load(response)
            path = self.root / 'output' / result['filename']
            paths.append(path)
            with self.request(result['download']) as response:
                self.assertEqual(response.read(), path.read_bytes())
                self.assertIn('attachment', response.headers['Content-Disposition'])
        self.assertNotEqual(*paths)
        self.assertTrue(all(p.is_file() for p in paths))
        self.assertEqual(self.source.read_bytes(), self.original)
        state = prep.read(self.folder / 'job.json')
        self.assertEqual(state['keep'], {'start': .5, 'end': 2.7})
        self.assertEqual(state['automatic_keep'], self.job['keep'])
        self.assertEqual((self.folder / 'report.txt').read_text().count('PHONE REVIEW'), 1)
        with patch.object(prep, 'run', side_effect=AssertionError('No tools should run')):
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                prep.crop('abcdefghijk')
            self.assertIn(str(paths[-1]), captured.getvalue())
            self.assertEqual(review.export('abcdefghijk', .5, 2.7), paths[-1])

    def test_save_without_encoding_then_cli_crop(self):
        original_export = review.export('abcdefghijk', .2, 2.8)
        original_bytes = original_export.read_bytes()
        with patch.object(review, 'encode', side_effect=AssertionError('Save must not encode')):
            with self.request('/api/save', {'id': 'abcdefghijk', 'start': .6, 'end': 2.5}) as response:
                result = json.load(response)
            self.assertEqual(result['keep'], {'start': .6, 'end': 2.5})
        with self.request('/api/job?id=abcdefghijk') as response:
            self.assertEqual(json.load(response)['keep'], result['keep'])
        with self.assertRaises(HTTPError) as error:
            self.request('/api/download?id=abcdefghijk')
        error.exception.close()
        self.assertEqual(prep.read(self.folder / 'job.json')['status'], 'ready')
        with contextlib.redirect_stdout(io.StringIO()):
            prep.crop('abcdefghijk')
        job = prep.read(self.folder / 'job.json')
        self.assertEqual(job['cropped_keep'], result['keep'])
        self.assertNotEqual(job['output'], str(original_export))
        self.assertEqual(original_export.read_bytes(), original_bytes)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertIn('Output:', (self.folder / 'report.txt').read_text())

    def test_invalid_save_preserves_times(self):
        with self.assertRaises(HTTPError) as error:
            self.request('/api/save', {'id': 'abcdefghijk', 'start': 2, 'end': 1})
        error.exception.close()
        self.assertEqual(prep.read(self.folder / 'job.json'), self.job)

    def test_invalid_cuts_and_failed_encode_preserve_state(self):
        for start, end in ((2, 1), (-1, 2), (0, 4), ('nan', 2), (0, 'inf')):
            with self.assertRaises(HTTPError) as error:
                self.request('/api/crop', {'id': 'abcdefghijk', 'start': start, 'end': end})
            self.assertEqual(error.exception.code, 400)
        with patch.object(review, 'encode', side_effect=ValueError('encode failed')):
            with self.assertRaises(HTTPError):
                self.request('/api/crop', {'id': 'abcdefghijk', 'start': .4, 'end': 2.7})
        self.assertEqual(prep.read(self.folder / 'job.json'), self.job)
        self.assertEqual(list((self.root / 'output').glob('*.m4a')), [])

    def test_preview_length_validation(self):
        for seconds in ('0', '-1', '61', 'nan', 'inf', 'bad'):
            with self.assertRaises(HTTPError) as error:
                self.request('/api/preview?id=abcdefghijk&start=0&end=3&edge=intro&seconds=' + seconds)
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
        with self.request('/api/preview?id=abcdefghijk&start=0&end=3&edge=outro&seconds=1') as response:
            path = self.root / 'short-preview.m4a'
            path.write_bytes(response.read())
        duration = float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
            'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(path)], text=True))
        self.assertAlmostEqual(duration, 1, delta=.03)

    def test_preview_defaults_and_custom_lengths(self):
        self.job['duration'] = 100
        prep.save(self.folder / 'job.json', self.job)
        calls = []
        def encode(source, start, end, destination, log):
            calls.append((start, end))
            destination.write_bytes(b'preview')
        with patch.object(review, 'encode', side_effect=encode):
            review.preview('abcdefghijk', 10, 90, 'intro')
            review.preview('abcdefghijk', 10, 90, 'outro')
            review.preview('abcdefghijk', 10, 90, 'intro', 12)
            review.preview('abcdefghijk', 10, 90, 'outro', 8)
            review.preview('abcdefghijk', 10, 13, 'intro', 60)
            review.preview('abcdefghijk', 10, 13, 'outro', 60)
        self.assertEqual(calls, [(10, 20), (85, 90), (10, 22), (82, 90), (10, 13), (10, 13)])


if __name__ == '__main__':
    unittest.main()
