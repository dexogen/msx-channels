import asyncio
import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

_CACHE_LOCKS = {}

EXTENSIONS = {'.mp4', '.m4v', '.mov', '.mkv', '.webm', '.avi', '.ts', '.m2ts', '.mpg', '.mpeg'}


def fingerprint(path: Path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


def discover(root: Path):
    if not root.is_dir():
        raise ValueError(f'Media directory does not exist: {root}')
    result = {}
    resolved = root.resolve()
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if any(part.startswith('.') for part in relative.parts):
            continue
        if path.suffix.lower() not in EXTENSIONS or path.is_symlink():
            continue
        try:
            if path.is_file() and path.resolve().is_relative_to(resolved):
                result[relative.as_posix()] = fingerprint(path)
        except FileNotFoundError:
            continue
    return result


def channel_id(relative: str, rotation: int):
    return hashlib.sha256(relative.encode()).hexdigest()[:16] + f'-r{rotation}'


def content_hash(path: Path):
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


async def terminate(process):
    if process is not None and process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


async def command(*args, timeout=None):
    process = await asyncio.create_subprocess_exec(
        *map(str, args), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        if process.returncode:
            raise ValueError(stderr.decode(errors='replace')[-1500:].strip() or f'Exit {process.returncode}')
        return stdout
    finally:
        await terminate(process)


def video_filter(rotation: int, fps: int):
    # Чёрная подложка того же размера учитывает alpha и autorotate исходника.
    rotation_filter = {0: '', 90: 'transpose=clock,', 180: 'hflip,vflip,', 270: 'transpose=cclock,'}[rotation]
    return (
        '[0:v:0]split[fg][bg];[bg]lutrgb=r=0:g=0:b=0,format=rgb24[black];'
        '[black][fg]overlay=shortest=1:format=auto,' + rotation_filter +
        "scale=w='if(gte(iw,ih),min(iw,1920),min(iw,1080))':"
        "h='if(gte(iw,ih),min(ih,1080),min(ih,1920))':"
        f'force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1,fps={fps},format=yuv420p[v]'
    )


@dataclass(frozen=True)
class Asset:
    key: str
    directory: Path
    segments: tuple[tuple[float, str], ...]

    @property
    def thumbnail(self):
        return self.directory / 'thumbnail.jpg'


def read_asset(directory: Path):
    parts = []
    duration = None
    for line in (directory / 'index.m3u8').read_text().splitlines():
        if line.startswith('#EXTINF:'):
            duration = float(line.split(':', 1)[1].split(',')[0])
        elif line and not line.startswith('#') and duration is not None:
            if Path(line).name != line or not (directory / line).is_file() or duration <= 0:
                raise ValueError('Invalid cached segment')
            parts.append((duration, line))
            duration = None
    if not parts or not (directory / 'thumbnail.jpg').is_file():
        raise ValueError('Incomplete prepared video')
    return Asset(directory.name, directory, tuple(parts))


async def prepare(path: Path, rotation: int, settings: Settings):
    digest = await asyncio.to_thread(content_hash, path)
    profile = f'hls-v2:{rotation}:{settings.fps}:{settings.crf}'
    key = hashlib.sha256(f'{digest}:{profile}'.encode()).hexdigest()
    lock = _CACHE_LOCKS.setdefault(str(settings.cache / key), asyncio.Lock())
    async with lock:
        return await _prepare_locked(path, rotation, settings, key)


async def _prepare_locked(path, rotation, settings, key):
    target = settings.cache / key
    if target.is_dir():
        return read_asset(target)
    probe = json.loads(await command(
        'ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
        '-show_streams', '-show_format', '-of', 'json', path, timeout=30))
    video = next((s for s in probe.get('streams', []) if s['codec_type'] == 'video'
                  and not s.get('disposition', {}).get('attached_pic')), None)
    if not video:
        raise ValueError('No video stream found')
    if video.get('color_transfer') in ('smpte2084', 'arib-std-b67'):
        raise ValueError('HDR input: convert to SDR before adding this file')
    audio = next((s for s in probe['streams'] if s['codec_type'] == 'audio'), None)
    temporary = settings.cache / f'.{key}.tmp'
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir()
    args = ['ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'error', '-y',
            '-threads', str(settings.threads), '-protocol_whitelist', 'file,pipe', '-i', str(path)]
    if not audio:
        args += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
    filters = video_filter(rotation, settings.fps).replace('[0:v:0]', f'[0:{video["index"]}]', 1)
    args += ['-filter_complex_threads', '1', '-filter_complex', filters,
             '-map', '[v]', '-map', f'0:{audio["index"]}' if audio else '1:a:0',
             '-map_metadata', '-1', '-c:v', 'libx264', '-preset', 'fast',
             '-crf', str(settings.crf), '-profile:v', 'high', '-level:v', '4.2',
             '-maxrate', '8M', '-bufsize', '16M', '-g', str(settings.fps * 2),
             '-keyint_min', str(settings.fps * 2), '-sc_threshold', '0',
             '-threads', str(settings.threads), '-c:a', 'aac', '-b:a', '128k',
             '-ac', '2', '-ar', '48000', '-af', 'apad', '-shortest',
             '-f', 'hls', '-hls_time', '2', '-hls_list_size', '0', '-hls_playlist_type', 'vod',
             '-hls_flags', 'independent_segments', '-hls_segment_options', 'mpegts_flags=+initial_discontinuity',
             '-hls_segment_filename', str(temporary / 'segment-%06d.ts'), str(temporary / 'index.m3u8')]
    try:
        await command(*args)
        await command('ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'error', '-y',
                      '-i', temporary / 'index.m3u8', '-vf', 'scale=480:-2', '-frames:v', '1',
                      '-update', '1', temporary / 'thumbnail.jpg')
        read_asset(temporary)
        temporary.replace(target)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return read_asset(target)
