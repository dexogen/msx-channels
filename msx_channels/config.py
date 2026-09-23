import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    media: Path = Path('/media')
    cache: Path = Path('/data/cache')
    database: Path = Path('/data/channels.sqlite3')
    public_url: str = ''
    port: int = 8080
    admin_user: str = 'admin'
    admin_password: str = ''
    scan_interval: float = 5
    settle_seconds: float = 5
    fps: int = 30
    crf: int = 20
    threads: int = 2
    prepare_jobs: int = 1
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024

    @classmethod
    def from_env(cls):
        value = cls(
            media=Path(os.getenv('MEDIA_DIR', '/media')),
            cache=Path(os.getenv('CACHE_DIR', '/data/cache')),
            database=Path(os.getenv('DATABASE', '/data/channels.sqlite3')),
            public_url=os.getenv('PUBLIC_URL', '').rstrip('/'),
            port=int(os.getenv('PORT', '8080')),
            admin_user=os.getenv('ADMIN_USER', 'admin'),
            admin_password=os.getenv('ADMIN_PASSWORD', ''),
            scan_interval=float(os.getenv('SCAN_INTERVAL', '5')),
            settle_seconds=float(os.getenv('SETTLE_SECONDS', '5')),
            fps=int(os.getenv('OUTPUT_FPS', '30')),
            crf=int(os.getenv('CRF', '20')),
            threads=int(os.getenv('FFMPEG_THREADS', '2')),
            prepare_jobs=int(os.getenv('PREPARE_JOBS', '1')),
            max_upload_bytes=int(os.getenv('MAX_UPLOAD_BYTES', str(2 * 1024 * 1024 * 1024))),
        )
        if len(value.admin_password) < 12:
            raise ValueError('Set ADMIN_PASSWORD to at least 12 characters')
        if value.scan_interval <= 0 or value.settle_seconds < 0:
            raise ValueError('Invalid scan/settle interval')
        if not (1 <= value.fps <= 60 and 0 <= value.crf <= 40):
            raise ValueError('OUTPUT_FPS must be 1..60; CRF must be 0..40')
        if value.threads < 1 or value.prepare_jobs < 1 or not 1 <= value.port <= 65535:
            raise ValueError('Invalid threads, jobs or port')
        if value.max_upload_bytes < 1:
            raise ValueError('MAX_UPLOAD_BYTES must be positive')
        if value.public_url:
            url = urlsplit(value.public_url)
            if url.scheme not in ('http', 'https') or not url.netloc or url.username or url.password or url.path or url.query or url.fragment:
                raise ValueError('PUBLIC_URL must be an HTTP(S) origin, without a path or credentials')
        return value
