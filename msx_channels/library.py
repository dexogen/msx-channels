import asyncio
import logging
import time
from pathlib import Path

from .config import Settings
from .media import discover, fingerprint, prepare
from .store import Store
from .timeline import Timeline

log = logging.getLogger(__name__)


class Library:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = None
        self.rows = {}
        self.timelines = {}
        self.observed = {}
        self.variants = {}
        self.assets = {}
        self.scan_error = ''
        self.tasks = []
        self.semaphore = asyncio.Semaphore(settings.prepare_jobs)

    async def start(self):
        self.settings.cache.mkdir(parents=True, exist_ok=True)
        if not self.settings.media.is_dir():
            raise ValueError('MEDIA_DIR must be an existing readable directory')
        self.store = Store(self.settings.database)
        self.refresh_channels()
        self.tasks = [asyncio.create_task(self.watch()), asyncio.create_task(self.clock())]

    def refresh_channels(self):
        self.rows = {row['id']: row for row in self.store.all()}
        for identifier in list(self.timelines):
            if identifier not in self.rows:
                del self.timelines[identifier]

    async def stop(self):
        tasks = self.tasks + [v['task'] for v in self.variants.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.store:
            self.store.close()

    async def watch(self):
        while True:
            try:
                await self.reconcile()
                self.scan_error = ''
            except Exception as error:
                self.scan_error = str(error)
                log.exception('Media scan failed')
            await asyncio.sleep(self.settings.scan_interval)

    async def reconcile(self):
        files = await asyncio.to_thread(discover, self.settings.media)
        now = time.monotonic()
        for name in list(self.observed):
            if name not in files:
                del self.observed[name]
        wanted = {}
        for name, signature in files.items():
            previous = self.observed.get(name)
            if previous is None or previous[0] != signature:
                self.observed[name] = (signature, now)
            if now - self.observed[name][1] < self.settings.settle_seconds or signature[0] == 0:
                continue
            rotations = {0} | {row['rotation'] for row in self.rows.values() if row['video'] == name}
            for rotation in rotations:
                wanted[(name, rotation)] = signature
        for key, variant in list(self.variants.items()):
            if key not in wanted or variant['signature'] != wanted[key]:
                variant['task'].cancel()
                await asyncio.gather(variant['task'], return_exceptions=True)
                del self.variants[key]
        for key, signature in wanted.items():
            if key not in self.variants:
                variant = {'signature': signature, 'state': 'preparing', 'error': '', 'asset': None}
                self.variants[key] = variant
                variant['task'] = asyncio.create_task(self.prepare_variant(key, variant))

    async def prepare_variant(self, key, variant):
        name, rotation = key
        while True:
            try:
                async with self.semaphore:
                    asset = await prepare(self.settings.media / name, rotation, self.settings)
                if fingerprint(self.settings.media / name) != variant['signature']:
                    raise ValueError('Input changed during preparation; waiting for a stable copy')
                self.assets[asset.key] = asset
                variant.update(state='ready', asset=asset, error='')
                log.info('Video ready: %s, rotation=%s', name, rotation)
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                variant.update(state='error', error=str(error)[-1500:])
                log.warning('%s: %s', name, variant['error'])
                await asyncio.sleep(60)

    async def clock(self):
        while True:
            for identifier, row in self.rows.items():
                variant = self.variants.get((row['video'], row['rotation']))
                asset = variant.get('asset') if variant else None
                if asset:
                    if identifier not in self.timelines:
                        self.timelines[identifier] = Timeline(asset, source=row['video'])
                    else:
                        self.timelines[identifier].select(asset, source=row['video'])
            for timeline in self.timelines.values():
                timeline.tick()
            await asyncio.sleep(0.25)

    def channels(self):
        result = []
        for identifier, row in self.rows.items():
            variant = self.variants.get((row['video'], row['rotation']))
            timeline = self.timelines.get(identifier)
            state = variant['state'] if variant else 'waiting'
            error = variant['error'] if variant else ''
            if row['video'] not in self.observed:
                state, error = 'missing', 'Source file is missing; any already playing cached video is retained'
            if variant and variant.get('asset') and timeline:
                state = 'running' if timeline.asset.key == variant['asset'].key else 'switching'
            result.append({**row, 'state': state, 'error': error, 'playing': timeline is not None,
                           'active_asset': timeline.asset.key if timeline else None})
        return result

    def videos(self):
        return [{'file': name, 'title': Path(name).stem,
                 'state': self.variants.get((name, 0), {}).get('state', 'settling'),
                 'error': self.variants.get((name, 0), {}).get('error', '')}
                for name in sorted(self.observed)]
