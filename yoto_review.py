"""A small, local phone UI. No third-party packages or external services."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import tempfile
import threading
from urllib.parse import parse_qs, quote, urlsplit

import yoto_prep as prep


def load_job(ident):
    if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", ident):
        raise ValueError("Invalid job ID.")
    folder = (prep.ROOT / 'jobs' / ident).resolve()
    job = prep.read(folder / 'job.json')
    source = Path(job['source']).resolve()
    if source.parent != folder or not source.is_file():
        raise ValueError("The saved source audio is missing.")
    return folder, job, source


def validate_cut(job, start, end):
    if job.get('status') not in ('ready', 'cropped') or not job.get('keep'):
        raise ValueError("This job needs successful boundary detection before phone review.")
    start, end = float(start), float(end)
    if not all(math.isfinite(v) for v in (start, end)) or not 0 <= start < end <= job['duration']:
        raise ValueError("Use a start before the end, within the recording's duration.")
    return start, end


def encode(source, start, end, destination, log):
    # Accurate input seeking decodes/discards up to start, then re-encodes samples.
    prep.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-ss', f'{start:.6f}',
              '-i', source, '-t', f'{end - start:.6f}', '-map', '0:a:0', '-vn',
              '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', destination], log)


def preview(ident, start, end, edge, seconds=None):
    folder, job, source = load_job(ident)
    start, end = validate_cut(job, start, end)
    if edge not in ('intro', 'outro'):
        raise ValueError('Unknown preview edge.')
    seconds = float(seconds) if seconds is not None else (10 if edge == 'intro' else 5)
    if not math.isfinite(seconds) or not 1 <= seconds <= 60:
        raise ValueError('Preview length must be between 1 and 60 seconds.')
    if edge == 'intro':
        end = min(end, start + seconds)
    else:
        start = max(start, end - seconds)
    with tempfile.TemporaryDirectory(prefix='.preview-', dir=folder) as directory:
        path = Path(directory) / 'preview.m4a'
        encode(source, start, end, path, folder / 'review-audio.log')
        return path.read_bytes()


def persist_timings(folder, job, keep):
    job.setdefault('automatic_keep', dict(job['keep']))
    job.update(keep=dict(keep), manual_review=dict(keep))
    prep.save(folder / 'job.json', job)
    report_path = folder / 'report.txt'
    marker = '\nPHONE REVIEW\n'
    original = report_path.read_text() if report_path.exists() else job['title'] + '\n'
    text = original.split(marker)[0] + marker
    text += f"Approved: {keep['start']:.3f}s -> {keep['end']:.3f}s\n"
    text += f"Duration: {prep.stamp(keep['end'] - keep['start'])}\n"
    if job.get('cropped_keep') == keep and job.get('output'):
        text += f"Output: {job['output']}\n"
    else:
        text += f"Timings saved. Crop later: yoto-prep crop {job['id']}\n"
    report_path.write_text(text)


def save_timings(ident, start, end):
    folder, _, _ = load_job(ident)
    with prep.lock(folder):
        _, job, _ = load_job(ident)
        start, end = validate_cut(job, start, end)
        keep = {'start': start, 'end': end}
        already_cropped = job.get('cropped_keep') == keep and Path(job.get('output', '')).is_file()
        job['status'] = 'cropped' if already_cropped else 'ready'
        persist_timings(folder, job, keep)
        return keep


def export(ident, start, end):
    folder, _, _ = load_job(ident)
    with prep.lock(folder):
        _, job, source = load_job(ident)
        start, end = validate_cut(job, start, end)
        keep = {'start': start, 'end': end}
        output_dir = prep.ROOT / 'output'
        output_dir.mkdir(exist_ok=True)
        digest = hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:12]
        title = re.sub(r'[^\w .-]+', '_', job['title']).strip(' .')[:120] or ident
        destination = output_dir / f'{title} [{ident}] [{digest}].m4a'
        if not destination.exists():
            with tempfile.TemporaryDirectory(prefix='.crop-', dir=output_dir) as directory:
                temporary = Path(directory) / 'audio.m4a'
                encode(source, start, end, temporary, folder / 'review-audio.log')
                os.link(temporary, destination)
        job.update(cropped_keep=dict(keep), status='cropped', output=str(destination))
        persist_timings(folder, job, keep)
        return destination


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, token):
        self.token = token
        self.audio_slots = threading.BoundedSemaphore(2)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # URLs contain an access token. Don't put them in request logs.
        pass

    def send_bytes(self, data, content_type='application/json', status=200, headers=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; media-src 'self'; frame-ancestors 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, data, status=200):
        self.send_bytes(json.dumps(data).encode(), status=status)

    def dispatch(self, post=False):
        parsed = urlsplit(self.path)
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        token = self.headers.get('X-Review-Token') or query.get('token', '')
        if not secrets.compare_digest(token, self.server.token):
            self.send_json({'error': 'Open the full review link printed by yoto-prep serve.'}, 403)
            return
        if post:
            # Browser mutations require a custom header, so cross-site forms can't submit them.
            if self.headers.get('X-Review-Token') != self.server.token:
                self.send_json({'error': 'Missing review token header.'}, 403)
                return
            if parsed.path not in ('/api/crop', '/api/save'):
                self.send_json({'error': 'Not found'}, 404)
                return
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 4096:
                raise ValueError('Invalid request size.')
            body = json.loads(self.rfile.read(length))
            if parsed.path == '/api/save':
                keep = save_timings(body['id'], body['start'], body['end'])
                self.send_json({'keep': keep, 'command': 'yoto-prep crop ' + body['id']})
                return
            with self.server.audio_slots:
                path = export(body['id'], body['start'], body['end'])
            self.send_json({'filename': path.name, 'download': '/api/download?id=' + quote(body['id'])})
        elif parsed.path == '/':
            self.send_bytes(Path(__file__).with_name('review.html').read_bytes(), 'text/html; charset=utf-8')
        elif parsed.path == '/api/jobs':
            jobs = []
            for path in sorted((prep.ROOT / 'jobs').glob('*/job.json'), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    job = prep.read(path)
                    jobs.append({'id': path.parent.name, 'title': job['title'], 'status': job['status'],
                                 'ready': job.get('status') in ('ready', 'cropped') and bool(job.get('keep'))})
                except (ValueError, KeyError, OSError):
                    continue
            self.send_json(jobs)
        elif parsed.path == '/api/job':
            folder, job, _ = load_job(query['id'])
            text = (folder / 'report.txt').read_text() if (folder / 'report.txt').exists() else ''
            contexts = {}
            for kind in ('intro', 'outro'):
                edge = job.get('edges', {}).get(kind)
                if edge:
                    contexts[kind] = prep.words_from(prep.read(folder / f'{kind}.json'), edge['offset'], edge['duration'])
            self.send_json({'id': job['id'], 'title': job['title'], 'duration': job['duration'],
                            'keep': job.get('keep'), 'contexts': contexts, 'report': text})
        elif parsed.path == '/api/preview':
            with self.server.audio_slots:
                data = preview(query['id'], query['start'], query['end'], query['edge'], query.get('seconds'))
            self.send_bytes(data, 'audio/mp4')
        elif parsed.path == '/api/download':
            _, job, _ = load_job(query['id'])
            if job.get('cropped_keep') != job.get('keep'):
                raise ValueError('Saved timings have changed. Crop this job before downloading.')
            path = Path(job.get('output', '')).resolve()
            if path.parent != (prep.ROOT / 'output').resolve() or path.suffix != '.m4a' or not path.is_file():
                raise ValueError('Crop this job first.')
            self.send_bytes(path.read_bytes(), 'audio/mp4', headers={
                'Content-Disposition': "attachment; filename=\"audiobook.m4a\"; filename*=UTF-8''" + quote(path.name)})
        else:
            self.send_json({'error': 'Not found'}, 404)

    def handle_request(self, post=False):
        try:
            self.dispatch(post)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, TypeError, KeyError, OSError) as error:
            self.send_json({'error': str(error)}, 400)

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request(True)


def serve(host='0.0.0.0', port=8765, token=None):
    prep.require('ffmpeg')
    token = token or secrets.token_urlsafe(24)
    server = ReviewServer((host, port), token)
    address = host
    if host == '0.0.0.0':
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(('8.8.8.8', 80))
                address = sock.getsockname()[0]
        except OSError:
            address = socket.gethostname()
    print(f'Open on your phone (same Wi-Fi): http://{address}:{server.server_port}/?token={token}', flush=True)
    print('Leave this running while reviewing. Press Ctrl-C to stop.', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
