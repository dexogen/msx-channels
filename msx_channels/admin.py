import asyncio
import secrets
import time
import os
import tempfile
from collections import deque
from pathlib import Path

from aiohttp import web
from .media import EXTENSIONS


def add_admin(app, settings, library):
    sessions = {}
    failures = deque()

    def authorized(request):
        token = request.cookies.get('msx_session', '')
        now = time.monotonic()
        for old, expiry in list(sessions.items()):
            if expiry < now:
                del sessions[old]
        if token not in sessions:
            raise web.HTTPUnauthorized(text='Sign in to continue')

    def mutation(request, json_body=True):
        if request.headers.get('X-MSX-Admin') != '1':
            raise web.HTTPForbidden(text='Missing request header')
        if json_body and request.content_type != 'application/json':
            raise web.HTTPUnsupportedMediaType()
        if json_body and request.content_length and request.content_length > 16384:
            raise web.HTTPRequestEntityTooLarge(max_size=16384, actual_size=request.content_length)

    async def page(request):
        return web.FileResponse(Path(__file__).with_name('admin.html'))

    async def login(request):
        mutation(request)
        now = time.monotonic()
        while failures and failures[0] < now - 60:
            failures.popleft()
        if len(failures) >= 20:
            raise web.HTTPTooManyRequests(text='Try again in a minute')
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError('Expected an object')
        except ValueError:
            raise web.HTTPBadRequest(text='Invalid JSON')
        user = str(data.get('username', '')).encode()
        password = str(data.get('password', '')).encode()
        if not (secrets.compare_digest(user, settings.admin_user.encode()) and
                secrets.compare_digest(password, settings.admin_password.encode())):
            failures.append(now)
            raise web.HTTPUnauthorized(text='Invalid username or password')
        token = secrets.token_urlsafe(32)
        sessions[token] = now + 12 * 3600
        response = web.json_response({'ok': True})
        secure = settings.public_url.startswith('https://') or request.headers.get('X-Forwarded-Proto') == 'https' or request.secure
        response.set_cookie('msx_session', token, httponly=True, samesite='Strict', secure=secure,
                            max_age=12 * 3600, path='/admin')
        return response

    async def logout(request):
        mutation(request)
        sessions.pop(request.cookies.get('msx_session', ''), None)
        response = web.json_response({'ok': True})
        response.del_cookie('msx_session', path='/admin')
        return response

    async def status(request):
        authorized(request)
        return web.json_response({'videos': library.videos(), 'channels': library.channels(),
                                  'scan_error': library.scan_error})

    async def save(request):
        authorized(request)
        mutation(request)
        try:
            data = await request.json()
            title = ' '.join(str(data['title']).split())
            video = str(data['video'])
            rotation = int(data.get('rotation', 0))
        except (ValueError, KeyError, TypeError):
            raise web.HTTPBadRequest(text='Invalid channel data')
        if not title or len(title) > 120 or rotation not in (0, 90, 180, 270):
            raise web.HTTPBadRequest(text='Invalid title or rotation')
        if video not in library.observed:
            raise web.HTTPBadRequest(text='Choose a video from the library')
        identifier = request.match_info.get('id')
        try:
            identifier = library.store.save(title, video, rotation, identifier)
        except KeyError:
            raise web.HTTPNotFound()
        library.refresh_channels()
        return web.json_response({'id': identifier, 'url': f'/hls/{identifier}/index.m3u8'}, status=200 if request.method == 'PUT' else 201)

    async def delete(request):
        authorized(request)
        mutation(request)
        try:
            library.store.delete(request.match_info['id'])
        except KeyError:
            raise web.HTTPNotFound()
        library.refresh_channels()
        return web.json_response({'ok': True})

    async def upload(request):
        authorized(request)
        mutation(request, json_body=False)
        if request.content_type != 'multipart/form-data':
            raise web.HTTPUnsupportedMediaType(text='Send a multipart file field')
        reader = await request.multipart()
        part = await reader.next()
        if part is None or part.name != 'file' or not part.filename:
            raise web.HTTPBadRequest(text='Missing file')
        name = part.filename
        if (Path(name).name != name or '/' in name or '\\' in name or '\x00' in name
                or name.startswith('.') or Path(name).suffix.lower() not in EXTENSIONS):
            raise web.HTTPBadRequest(text='Unsupported filename or video extension')
        target = settings.media / name
        if target.exists():
            raise web.HTTPConflict(text='A file with this name already exists')
        temporary = None
        size = 0
        try:
            with tempfile.NamedTemporaryFile(dir=settings.media, prefix='.upload-', delete=False) as output:
                temporary = Path(output.name)
                while chunk := await part.read_chunk(size=1024 * 1024):
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise web.HTTPRequestEntityTooLarge(max_size=settings.max_upload_bytes, actual_size=size)
                    await asyncio.to_thread(output.write, chunk)
            if not size:
                raise web.HTTPBadRequest(text='Empty file')
            if await reader.next() is not None:
                raise web.HTTPBadRequest(text='Upload one file per request')
            os.chmod(temporary, 0o644)
            try:
                os.link(temporary, target)
            except FileExistsError:
                raise web.HTTPConflict(text='A file with this name already exists')
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
        return web.json_response({'file': name, 'bytes': size}, status=201)

    async def delete_video(request):
        authorized(request)
        mutation(request)
        try:
            data = await request.json()
            name = data['file']
        except (ValueError, KeyError, TypeError):
            raise web.HTTPBadRequest(text='Invalid file')
        if not isinstance(name, str) or name not in library.observed:
            raise web.HTTPNotFound()
        path = settings.media / name
        if not path.resolve().is_relative_to(settings.media.resolve()) or path.is_symlink():
            raise web.HTTPForbidden()
        in_use = [row['title'] for row in library.rows.values() if row['video'] == name
                  or (row['id'] in library.timelines and library.timelines[row['id']].source == name)]
        if in_use:
            raise web.HTTPConflict(text='Video is still used by: ' + ', '.join(in_use))
        try:
            path.unlink()
        except FileNotFoundError:
            raise web.HTTPNotFound()
        await library.reconcile()
        return web.json_response({'ok': True})

    app.router.add_get('/admin', page)
    app.router.add_get('/admin/', page)
    app.router.add_post('/admin/login', login)
    app.router.add_post('/admin/logout', logout)
    app.router.add_get('/admin/api/status', status)
    app.router.add_post('/admin/api/channels', save)
    app.router.add_put('/admin/api/channels/{id}', save)
    app.router.add_delete('/admin/api/channels/{id}', delete)
    app.router.add_post('/admin/api/videos', upload)
    app.router.add_delete('/admin/api/videos', delete_video)
