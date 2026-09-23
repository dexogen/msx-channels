import html
import logging
import re
from aiohttp import web
from . import __version__
from .admin import add_admin
from .config import Settings
from .library import Library


def origin(request, settings):
    if settings.public_url:
        return settings.public_url
    scheme = request.headers.get('X-Forwarded-Proto', request.scheme).split(',')[0].strip()
    if scheme not in ('http', 'https'):
        scheme = request.scheme
    return f'{scheme}://{request.host}'


def create_app(settings=None):
    settings = settings or Settings.from_env()
    library = Library(settings)

    async def lifecycle(app):
        await library.start()
        yield
        await library.stop()

    @web.middleware
    async def headers(request, handler):
        admin = request.path.startswith('/admin')
        if request.method == 'OPTIONS':
            response = web.Response(status=403 if admin else 204)
        else:
            try:
                response = await handler(request)
            except web.HTTPException as error:
                response = error
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
        if not admin:
            response.headers.update({
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
                'Access-Control-Allow-Headers': 'Range, Content-Type',
                'Access-Control-Expose-Headers': 'Content-Length, Content-Range',
            })
        else:
            response.headers.update({'X-Frame-Options': 'DENY', 'Referrer-Policy': 'same-origin'})
        return response

    async def health(request):
        alive = bool(library.tasks) and all(not task.done() for task in library.tasks)
        return web.json_response({'status': 'ok' if alive else 'error'}, status=200 if alive else 503)

    async def ready(request):
        count = len(library.timelines)
        return web.json_response({'ready': count, 'total': len(library.rows)}, status=200 if count else 503)

    async def start(request):
        return web.json_response({'name': 'MSX Channels', 'version': __version__,
                                  'parameter': f'content:{origin(request, settings)}/msx/channels.json'})

    async def catalog(request):
        base = origin(request, settings)
        items = [{'title': c['title'], 'playerLabel': c['title'],
                  'image': f"{base}/assets/{c['active_asset']}/thumbnail.jpg",
                  'action': f"video:{base}/hls/{c['id']}/index.m3u8",
                  'properties': {'progress:display': 'false', 'button:restart:display': 'false'}}
                 for c in library.channels() if c['playing']]
        if not items:
            items = [{'title': 'Нет готовых каналов', 'text': 'Создайте канал в админке и дождитесь подготовки видео.',
                      'action': f'content:{base}/msx/channels.json'}]
        return web.json_response({'name': 'MSX Channels', 'version': __version__, 'cache': False,
                                  'headline': 'MSX Channels', 'template': {'type': 'separate', 'layout': '0,0,4,3'},
                                  'items': items})

    async def playlist(request):
        base = origin(request, settings)
        lines = ['#EXTM3U']
        for c in library.channels():
            if c['playing']:
                lines += [f"#EXTINF:-1 group-title=\"MSX Channels\",{c['title']}", f"{base}/hls/{c['id']}/index.m3u8"]
        return web.Response(text='\n'.join(lines) + '\n', content_type='application/x-mpegURL')

    async def manifest(request):
        timeline = library.timelines.get(request.match_info['id'])
        if timeline is None:
            if request.match_info['id'] in library.rows:
                raise web.HTTPServiceUnavailable(text='Channel is preparing', headers={'Retry-After': '5'})
            raise web.HTTPNotFound()
        timeline.tick()
        return web.Response(text=timeline.manifest(origin(request, settings)), content_type='application/vnd.apple.mpegurl')

    async def asset_file(request):
        key, name = request.match_info['key'], request.match_info['name']
        if not re.fullmatch(r'[a-f0-9]{64}', key) or not re.fullmatch(r'segment-\d+\.ts|thumbnail\.jpg', name):
            raise web.HTTPNotFound()
        path = settings.cache / key / name
        if not path.is_file():
            raise web.HTTPNotFound()
        mime = 'image/jpeg' if name.endswith('.jpg') else 'video/mp2t'
        return web.FileResponse(path, headers={'Content-Type': mime})

    async def index(request):
        base = origin(request, settings)
        cards = []
        for c in library.channels():
            if c['playing']:
                cards.append(f"<article><h2>{html.escape(c['title'])}</h2><img src='/assets/{c['active_asset']}/thumbnail.jpg' alt=''><p><a href='/hls/{c['id']}/index.m3u8'>HLS-поток</a></p></article>")
        if not cards:
            cards.append('<p>Создайте канал в админке. После подготовки он появится здесь и в MSX.</p>')
        page = """<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width"><meta http-equiv="refresh" content="30">
<title>MSX Channels</title><style>
body{margin:3rem auto;max-width:1100px;padding:0 1rem;background:#101216;color:#eee;font:17px system-ui}
a{color:#9bcfff}code{color:#b8ead2}section{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:1rem}
article{padding:1rem;background:#20232b;border-radius:12px;overflow-wrap:anywhere}h2{font-size:1.2rem}
img{width:100%;height:230px;object-fit:contain;background:#000}p{line-height:1.7}
</style><h1>MSX Channels</h1><p>Media Station X → Settings → Start Parameter → Setup<br>
Адрес: <code>""" + html.escape(base.split('://', 1)[1]) + """</code>.
Для HTTPS включите замок.</p><p>Каждый канал непрерывно повторяет выбранный ролик.</p><section>"""
        page += ''.join(cards) + '</section><p><a href="/channels.m3u">M3U-плейлист</a> · <a href="/admin">Управление каналами</a></p></html>'
        return web.Response(text=page, content_type='text/html')

    app = web.Application(middlewares=[headers], client_max_size=settings.max_upload_bytes + 1024 * 1024)
    app.cleanup_ctx.append(lifecycle)
    for path, handler in [('/', index), ('/healthz', health), ('/readyz', ready),
                          ('/msx/start.json', start), ('/msx/channels.json', catalog), ('/channels.m3u', playlist),
                          ('/hls/{id}/index.m3u8', manifest), ('/segments/{key}/{name}', asset_file),
                          ('/assets/{key}/{name}', asset_file)]:
        app.router.add_get(path, handler)
    add_admin(app, settings, library)
    async def options(request):
        return web.Response(status=204)
    app.router.add_route('OPTIONS', '/{path:.*}', options)
    return app


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    settings = Settings.from_env()
    web.run_app(create_app(settings), host='0.0.0.0', port=settings.port, access_log=None, print=None)


if __name__ == '__main__':
    main()
