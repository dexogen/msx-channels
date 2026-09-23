import http.cookiejar
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')


def wait_for(function, timeout=60):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        try:
            result = function()
            if result:
                return result
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.25)
    raise AssertionError('Timed out waiting for service state')


def test_admin_live_switch_rotation_and_persistence(tmp_path):
    media, cache = tmp_path / 'media', tmp_path / 'cache'
    media.mkdir()
    for name, color, audio in [('Красный ролик.mp4', 'red', True), ('blue.mov', 'blue', False)]:
        args = ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'color={color}:s=160x120:r=24:d=4']
        if audio:
            args += ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=4']
        args += ['-c:v', 'libx264', '-pix_fmt', 'yuv420p']
        if audio:
            args += ['-c:a', 'aac', '-shortest']
        subprocess.run(args + [str(media / name)], check=True)
    (media / 'broken.mp4').write_text('not video')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    password = secrets.token_urlsafe(24)
    env = {**os.environ, 'MEDIA_DIR': str(media), 'CACHE_DIR': str(cache),
           'DATABASE': str(tmp_path / 'channels.db'), 'ADMIN_PASSWORD': password,
           'PORT': str(port), 'PUBLIC_URL': '', 'SCAN_INTERVAL': '0.25', 'SETTLE_SECONDS': '0.25'}
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    log = (tmp_path / 'service.log').open('w+')
    process = None

    def request(path, method='GET', body=None, authenticated=True, headers=None):
        h = {'X-MSX-Admin': '1', **(headers or {})}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            h['Content-Type'] = 'application/json'
        req = urllib.request.Request(base + path, data=payload, method=method, headers=h)
        return (opener.open if authenticated else urllib.request.urlopen)(req, timeout=5)

    def data(path='/admin/api/status', **kwargs):
        with request(path, **kwargs) as r:
            return json.load(r)

    def start():
        proc = subprocess.Popen([sys.executable, '-m', 'msx_channels.app'], env=env, stdout=log, stderr=log)
        wait_for(lambda: data('/healthz'))
        data('/admin/login', method='POST', body={'username': 'admin', 'password': password})
        return proc

    def upload(name, payload):
        boundary = '----upload-' + secrets.token_hex(10)
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                'Content-Type: video/mp4\r\n\r\n').encode() + payload + f'\r\n--{boundary}--\r\n'.encode()
        req = urllib.request.Request(base + '/admin/api/videos', data=body,
                                     headers={'X-MSX-Admin': '1', 'Content-Type': 'multipart/form-data; boundary=' + boundary})
        with opener.open(req) as r:
            return json.load(r)

    try:
        process = start()
        with pytest.raises(urllib.error.HTTPError) as error:
            request('/admin/api/status', authenticated=False)
        assert error.value.code == 401
        upload('uploaded.mp4', (media / 'Красный ролик.mp4').read_bytes())
        with pytest.raises(urllib.error.HTTPError) as error:
            upload('uploaded.mp4', b'duplicate')
        assert error.value.code == 409
        with pytest.raises(urllib.error.HTTPError) as error:
            upload('../escape.mp4', b'invalid')
        assert error.value.code == 400
        wait_for(lambda: sum(v['state'] == 'ready' for v in data()['videos']) == 3)
        wait_for(lambda: any(v['file'] == 'broken.mp4' and v['state'] == 'error' for v in data()['videos']))
        channel = data('/admin/api/channels', method='POST', body={'title': 'Test', 'video': 'uploaded.mp4'})
        identifier = channel['id']
        wait_for(lambda: data()['channels'][0]['state'] == 'running')
        with pytest.raises(urllib.error.HTTPError) as error:
            data('/admin/api/videos', method='DELETE', body={'file': 'uploaded.mp4'})
        assert error.value.code == 409
        url = channel['url']
        # Identical bytes share prepared segments, but only the selected file is in use.
        data('/admin/api/videos', method='DELETE', body={'file': 'Красный ролик.mp4'})
        assert not (media / 'Красный ролик.mp4').exists()
        assert (media / 'uploaded.mp4').exists()
        with request(url) as r:
            assert r.headers['Access-Control-Allow-Origin'] == '*'
            initial = r.read().decode()
        first_sequence = int(next(x.split(':')[1] for x in initial.splitlines() if x.startswith('#EXT-X-MEDIA-SEQUENCE:')))
        segment = next(x for x in initial.splitlines() if x.startswith('http'))
        with opener.open(urllib.request.Request(segment, headers={'Range': 'bytes=0-187'})) as r:
            assert r.status == 206 and len(r.read()) == 188
        decoder = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-xerror',
                                    '-i', base + url, '-t', '24', '-vf', 'fps=1,scale=1:1',
                                    '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(3)
        edited = data('/admin/api/channels/' + identifier, method='PUT', body={'title': 'Updated', 'video': 'blue.mov', 'rotation': 0})
        assert edited['id'] == identifier and edited['url'] == url
        raw, stderr = decoder.communicate(timeout=55)
        assert decoder.returncode == 0, stderr.decode()
        pixels = list(zip(raw[::3], raw[1::3], raw[2::3]))
        assert any(r > b + 100 for r, g, b in pixels[:6]), pixels
        assert any(b > r + 100 for r, g, b in pixels[-6:]), pixels
        with request(url) as r:
            current = r.read().decode()
        assert '#EXT-X-DISCONTINUITY' in current
        sequence = int(next(x.split(':')[1] for x in current.splitlines() if x.startswith('#EXT-X-MEDIA-SEQUENCE:')))
        assert sequence >= first_sequence
        data('/admin/api/channels/' + identifier, method='PUT', body={'title': 'Vertical', 'video': 'blue.mov', 'rotation': 90})
        wait_for(lambda: data()['channels'][0]['state'] == 'running' and data()['channels'][0]['rotation'] == 90)
        with request(url) as r:
            latest = r.read().decode()
        segment = [x for x in latest.splitlines() if x.startswith('http')][-1]
        probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type,width,height', '-of', 'json', segment]))
        video = next(s for s in probe['streams'] if s['codec_type'] == 'video')
        assert (video['width'], video['height']) == (120, 160)
        process.terminate()
        process.wait(timeout=15)
        process = start()
        wait_for(lambda: data()['channels'][0]['playing'])
        assert data()['channels'][0]['id'] == identifier
        data('/admin/api/channels/' + identifier, method='DELETE', body={})
        with pytest.raises(urllib.error.HTTPError) as error:
            request(url)
        assert error.value.code == 404
        assert (media / 'blue.mov').exists()
        data('/admin/api/videos', method='DELETE', body={'file': 'uploaded.mp4'})
        assert not (media / 'uploaded.mp4').exists()
        assert not list(media.glob('.upload-*'))
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=15)
        log.close()
