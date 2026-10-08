import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yoto_prep as app


def transcript(text, start=0, probability=0.95):
    words = [{"word": word, "start": start + i * 0.4,
              "end": start + i * 0.4 + 0.3, "probability": probability}
             for i, word in enumerate(text.split())]
    return {"segments": [{"start": start, "end": words[-1]["end"],
                          "text": text, "words": words, "avg_logprob": -0.1,
                          "no_speech_prob": 0.01}]}


class MatchingTests(unittest.TestCase):
    def detect(self, text, kind, offset=0, probability=0.95):
        return app.detect(app.words_from(transcript(text, 10, probability), offset, 240), kind)

    def test_intro_normalization_and_word_end(self):
        for text in ('Hello YOU, too SILLY goose! Once upon a time.',
                     'Hello you two silly goose Once upon a time.',
                     'Hello you to silly gose Once upon a time.'):
            with self.subTest(text=text):
                match, error = self.detect(text, 'intro')
                self.assertIsNone(error)
                self.assertAlmostEqual(match['end'], 11.9)

    def test_outro_contractions_and_offset(self):
        for text in ('The end. Isn’t reading fun?', 'The end. IS NOT reading fun!',
                     "The end. isn 't reading fun?", 'The end. isnt reeding fun?', "The end. is n't reading fun?"):
            with self.subTest(text=text):
                match, error = self.detect(text, 'outro', offset=360)
                self.assertIsNone(error)
                self.assertAlmostEqual(match['start'], 370.8)

    def test_optional_wow_and_preview(self):
        for prefix in ('Wow', 'WOW!', 'wow,'):
            data = transcript(f"The end. {prefix} isn't reading fun?", start=10)
            words = app.words_from(data, 360, 240)
            match, error = app.detect(words, 'outro')
            self.assertIsNone(error)
            self.assertAlmostEqual(match['start'], 370.8)
            self.assertTrue(match['text'].startswith(prefix))
            self.assertEqual(app.context(words, match['start'], False), 'The end.')

    def test_uncertain_wow_fails_safely(self):
        for mode in ('probability', 'zero_duration', 'invalid_time'):
            data = transcript("Wow isn't reading fun?")
            wow = data['segments'][0]['words'][0]
            if mode == 'probability':
                wow['probability'] = 0.69
            elif mode == 'zero_duration':
                wow['end'] = wow['start']
            else:
                wow['start'] = float('nan')
            match, error = app.detect(app.words_from(data, 0, 60), 'outro')
            self.assertIsNone(match, mode)
            self.assertIn('uncertain', error)

    def test_distant_and_unrelated_wow_stays_in_book(self):
        data = transcript("Wow isn't reading fun?")
        for word in data['segments'][0]['words'][1:]:
            word['start'] += 3
            word['end'] += 3
        match, error = app.detect(app.words_from(data, 0, 60), 'outro')
        self.assertIsNone(error)
        self.assertAlmostEqual(match['start'], 3.4)
        self.assertEqual(match['text'], "isn't reading fun?")
        match, error = self.detect("Wow the end. Isn't reading fun?", 'outro')
        self.assertIsNone(error)
        self.assertAlmostEqual(match['start'], 11.2)

    def test_repeated_outros_remain_ambiguous(self):
        match, error = self.detect("Wow isn't reading fun? Isn't reading fun?", 'outro')
        self.assertIsNone(match)
        self.assertIn('ambiguous', error)

    def test_reject_unsafe_matches(self):
        for text, kind in [('you silly goose', 'intro'), ('you too silly good', 'intro'),
                           ('is reading fun', 'outro'), ("isn't really fun", 'outro'),
                           ("isn't reading funny", 'outro'), ('nothing here', 'intro')]:
            with self.subTest(text=text):
                match, error = self.detect(text, kind)
                self.assertIsNone(match)
                self.assertIn('not found', error)
        self.assertIsNone(self.detect('you too silly goose', 'intro', probability=0.4)[0])
        self.assertIn('ambiguous', self.detect('you too silly goose you too silly goose', 'intro')[1])

    def test_reject_bad_alignment_and_silence(self):
        for mode in ('zero', 'nan', 'silence', 'long_gap', 'no_words'):
            data = transcript('you too silly goose')
            segment = data['segments'][0]
            if mode == 'zero':
                segment['words'][-1]['end'] = segment['words'][-1]['start']
            elif mode == 'nan':
                segment['words'][0]['start'] = float('nan')
            elif mode == 'silence':
                segment['no_speech_prob'] = 0.9
            elif mode == 'long_gap':
                segment['words'][-1].update(start=20, end=21)
            else:
                segment.pop('words')
            self.assertIsNone(app.detect(app.words_from(data, 0, 60), 'intro')[0], mode)

    def test_real_exact_intro_with_one_weak_word(self):
        path = Path(__file__).parent / 'fixtures/intro_low_confidence_word.json'
        data = json.loads(path.read_text())
        words = app.words_from(data, 0, 60)
        match, error = app.detect(words, 'intro')
        self.assertIsNone(error)
        self.assertEqual(match['end'], 12.1)
        self.assertTrue(app.context(words, match['end'], True).startswith("It's time to read"))

    def test_weak_word_exception_stays_conservative(self):
        for mode in ('weak_cut', 'two_weak', 'very_weak', 'fuzzy', 'ambiguous'):
            data = transcript('you too silly goose')
            words = data['segments'][0]['words']
            words[2]['probability'] = 0.3
            if mode == 'weak_cut':
                words[2]['probability'] = 0.95
                words[-1]['probability'] = 0.3
            elif mode == 'two_weak':
                words[1]['probability'] = 0.3
            elif mode == 'very_weak':
                words[2]['probability'] = 0.1
            elif mode == 'fuzzy':
                words[2]['word'] = 'sily'
            else:
                data['segments'] += transcript('you too silly goose', start=10)['segments']
            match, error = app.detect(app.words_from(data, 0, 60), 'intro')
            self.assertIsNone(match, mode)
            self.assertIsNotNone(error, mode)

    def test_context_correct_side(self):
        words = app.words_from(transcript('you too silly goose Book starts here', 10), 0, 60)
        match, _ = app.detect(words, 'intro')
        self.assertEqual(app.context(words, match['end'], True), 'Book starts here')
        words = app.words_from(transcript("The story ends isnt reading fun", 10), 120, 240)
        match, _ = app.detect(words, 'outro')
        self.assertEqual(app.context(words, match['start'], False), 'The story ends')

    def test_url_and_time(self):
        self.assertEqual(app.video_id('https://youtu.be/abcdefghijk?si=123'), 'abcdefghijk')
        self.assertEqual(app.video_id('https://www.youtube.com/watch?v=abcdefghijk&list=xyz'), 'abcdefghijk')
        for url in ('https://youtube.com/playlist?list=xyz', 'https://example.com/watch?v=abcdefghijk'):
            with self.assertRaises(ValueError):
                app.video_id(url)
        self.assertEqual(app.stamp(59.99), '01:00.0')
        self.assertEqual(app.stamp(503.8), '08:23.8')


class RefinementTests(unittest.TestCase):
    def test_quiet_gap_selection_and_protected_word(self):
        words = app.words_from(transcript('you too silly goose Book starts', 10), 0, 60)
        match, _ = app.detect(words, 'intro')
        original = match['end']
        with patch.object(app, 'quiet_gaps', return_value=[(original - .1, original + .07)]):
            result = app.refine_boundary(Path('source'), match, words, 'intro', 60, Path('log'))
        self.assertEqual(result['method'], 'silence')
        self.assertLess(result['selected'], words[4]['start'])
        self.assertLessEqual(abs(result['selected'] - original), .35)

    def test_no_silence_keeps_original(self):
        match = {'start': 5, 'end': 6}
        with patch.object(app, 'quiet_gaps', return_value=[]):
            result = app.refine_boundary(Path('source'), match, [], 'outro', 10, Path('log'))
        self.assertEqual(result['selected'], 5)
        self.assertEqual(result['method'], 'whisper')

    def test_gap_cannot_remove_preceding_book_word(self):
        words = [{'start': 4.5, 'end': 4.95}]
        with patch.object(app, 'quiet_gaps', return_value=[(4.7, 5.1)]):
            result = app.refine_boundary(Path('source'), {'start': 5, 'end': 6}, words,
                                         'outro', 10, Path('log'))
        self.assertGreater(result['selected'], 4.95)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg required')
    def test_real_silence_and_continuous_audio(self):
        import math
        import struct
        import wave
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'signal.wav'
            for with_gap in (True, False):
                with wave.open(str(source), 'wb') as wav:
                    wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                    wav.writeframes(b''.join(struct.pack('<h', 0 if with_gap and .8 <= i / 16000 < 1.2
                        else int(10000 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(32000)))
                result = app.refine_boundary(source, {'start': .5, 'end': .85}, [],
                                             'intro', 2, Path(directory) / 'analysis.log')
                if with_gap:
                    self.assertEqual(result['method'], 'silence')
                    self.assertAlmostEqual(result['selected'], 1, delta=.01)
                else:
                    self.assertEqual(result['method'], 'whisper')
                    self.assertEqual(result['selected'], .85)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.patcher = patch.object(app, 'ROOT', self.root)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.folder = self.root / 'jobs' / 'abcdefghijk'
        self.calls = []
        silence_patch = patch.object(app, 'quiet_gaps', return_value=[])
        self.silence_mock = silence_patch.start()
        self.addCleanup(silence_patch.stop)
        self.outro_text = "They lived happily ever after. Isn't reading fun?"

    def fake_run(self, args, log):
        self.calls.append(args)
        if args[0] == 'yt-dlp':
            source = self.folder / 'source.opus'
            source.write_bytes(b'audio placeholder')
            app.save(self.folder / 'source.info.json', {'title': 'A Book'})
            return str(source)
        if args[0] == 'ffprobe':
            return '600'
        if args[0] == 'whisper':
            kind = Path(args[1]).stem
            text = 'you too silly goose Once upon a time the story began' if kind == 'intro' else self.outro_text
            app.save(self.folder / f'{kind}.json', transcript(text, 15 if kind == 'intro' else 100))
        if args[0] == 'ffmpeg':
            Path(args[-1]).write_bytes(b'audio placeholder')
        return ''

    def test_review_then_crop_never_repeats_work(self):
        with patch.object(app, 'run', side_effect=self.fake_run), patch('sys.stdout', new_callable=io.StringIO):
            app.prepare('https://youtu.be/abcdefghijk')
            job = app.read(self.folder / 'job.json')
            self.assertEqual(job['status'], 'ready')
            self.assertAlmostEqual(job['keep']['start'], 16.5)
            self.assertAlmostEqual(job['keep']['end'], 462.0)
            self.assertFalse((self.root / 'output').exists())
            edge_calls = [c for c in self.calls if c[0] == 'ffmpeg']
            self.assertEqual([c[c.index('-t') + 1] for c in edge_calls], [60, 240])
            self.assertEqual([c[c.index('-ss') + 1] for c in edge_calls], [0, 360])
            self.assertIn('Once upon a time', (self.folder / 'report.txt').read_text())
            self.assertIn('They lived happily ever after.', (self.folder / 'report.txt').read_text())
            self.calls.clear()
            self.silence_mock.reset_mock()
            app.crop('abcdefghijk')
            self.silence_mock.assert_not_called()
            self.assertEqual([c[0] for c in self.calls], ['ffmpeg'])
            self.assertTrue((self.folder / 'source.opus').exists())
            job = app.read(self.folder / 'job.json')
            self.assertTrue(Path(job['output']).is_file())
            self.calls.clear()
            app.crop('abcdefghijk')
            self.assertEqual(self.calls, [])

    def test_auto_and_failed_detection(self):
        with patch.object(app, 'run', side_effect=self.fake_run), patch('sys.stdout', new_callable=io.StringIO):
            app.prepare('https://youtu.be/abcdefghijk', automatic=True)
            self.assertEqual(app.read(self.folder / 'job.json')['status'], 'cropped')
            # A new failed review must invalidate previously approved timestamps.
            (self.folder / 'outro.json').unlink()
            self.outro_text = 'This recording has no ending phrase'
            with self.assertRaisesRegex(ValueError, 'No safe crop'):
                app.prepare('https://youtu.be/abcdefghijk', automatic=True)
            self.assertNotIn('keep', app.read(self.folder / 'job.json'))
            self.assertIn('NOT READY TO CROP', (self.folder / 'report.txt').read_text())
            self.assertTrue((self.folder / 'outro.json').is_file())
            with self.assertRaisesRegex(ValueError, 'no safe crop'):
                app.crop('abcdefghijk')

    def test_auto_rejects_uncertain_wow(self):
        self.outro_text = "Wow isn't reading fun?"
        original_run = self.fake_run
        def run_with_weak_wow(args, log):
            result = original_run(args, log)
            if args[0] == 'whisper' and Path(args[1]).stem == 'outro':
                path = self.folder / 'outro.json'
                data = app.read(path)
                data['segments'][0]['words'][0]['probability'] = 0.6
                app.save(path, data)
            return result
        with patch.object(app, 'run', side_effect=run_with_weak_wow), patch('sys.stdout', new_callable=io.StringIO):
            with self.assertRaisesRegex(ValueError, 'No safe crop'):
                app.prepare('https://youtu.be/abcdefghijk', automatic=True)
        self.assertFalse((self.root / 'output').exists())
        self.assertIn('uncertain', (self.folder / 'report.txt').read_text())

    def test_tool_failure_retains_report(self):
        with patch.object(app, 'run', side_effect=ValueError('download failed')), patch('sys.stdout', new_callable=io.StringIO):
            with self.assertRaisesRegex(ValueError, 'Preparation failed'):
                app.prepare('https://youtu.be/abcdefghijk', automatic=True)
        self.assertIn('download failed', (self.folder / 'report.txt').read_text())
        self.assertEqual(app.read(self.folder / 'job.json')['status'], 'failed')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg required')
    def test_actual_ffmpeg_crop(self):
        self.folder.mkdir(parents=True)
        source = self.folder / 'source.wav'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=3', str(source)], check=True)
        original = source.read_bytes()
        app.save(self.folder / 'job.json', {'id': 'abcdefghijk', 'title': 'Test / Book',
                 'status': 'ready', 'duration': 3, 'source': str(source),
                 'keep': {'start': 0.431, 'end': 2.172}})
        with patch('sys.stdout', new_callable=io.StringIO):
            app.crop('abcdefghijk')
        job = app.read(self.folder / 'job.json')
        duration = float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
                         'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', job['output']], text=True))
        self.assertAlmostEqual(duration, 1.741, delta=0.03)
        self.assertEqual(source.read_bytes(), original)
        # A changed crop cannot silently overwrite an existing export.
        job['keep']['start'] = 0.7
        app.save(self.folder / 'job.json', job)
        with self.assertRaisesRegex(ValueError, 'Output already exists'):
            app.crop('abcdefghijk')


if __name__ == '__main__':
    unittest.main()
