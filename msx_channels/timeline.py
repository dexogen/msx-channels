"""Непрерывная HLS-шкала с заменой источника после опубликованных сегментов."""
import math
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

from .media import Asset


@dataclass(frozen=True)
class Entry:
    sequence: int
    start: float
    duration: float
    asset: str
    segment: str
    discontinuity: int
    boundary: bool


class Timeline:
    def __init__(self, asset: Asset, now=None, source=''):
        now = time.time() if now is None else now
        self.entries = deque()
        self.asset = self.desired = asset
        self.source = self.desired_source = source
        self.position = 0
        self.next_start = now - 6
        self.sequence = int(now * 1000)
        self.discontinuity = 0
        self.target_duration = 2
        self.tick(now)

    def select(self, asset: Asset, source=None):
        self.desired = asset
        if source is not None:
            self.desired_source = source

    def tick(self, now=None):
        now = time.time() if now is None else now
        if self.next_start < now - 60:
            self.next_start = now - 6
            self.position = 0
        while self.next_start < now + 4:
            changed = self.desired.key != self.asset.key
            if changed:
                self.asset = self.desired
                self.position = 0
            self.source = self.desired_source
            boundary = bool(self.entries) and self.position == 0
            if boundary:
                self.discontinuity += 1
            duration, filename = self.asset.segments[self.position]
            self.target_duration = max(self.target_duration, math.ceil(duration))
            self.entries.append(Entry(self.sequence, self.next_start, duration, self.asset.key,
                                      filename, self.discontinuity, boundary))
            self.sequence += 1
            self.next_start += duration
            self.position = (self.position + 1) % len(self.asset.segments)
        # Время окна сохраняется и для коротких роликов с частыми границами.
        while len(self.entries) > 3 and self.entries[1].start < now - 20:
            self.entries.popleft()

    def manifest(self, base: str):
        first = self.entries[0]
        lines = ['#EXTM3U', '#EXT-X-VERSION:6', f'#EXT-X-TARGETDURATION:{self.target_duration}',
                 f'#EXT-X-MEDIA-SEQUENCE:{first.sequence}',
                 f'#EXT-X-DISCONTINUITY-SEQUENCE:{first.discontinuity - int(first.boundary)}',
                 '#EXT-X-INDEPENDENT-SEGMENTS']
        for entry in self.entries:
            if entry.boundary:
                lines.append('#EXT-X-DISCONTINUITY')
            date = datetime.fromtimestamp(entry.start, timezone.utc).isoformat(timespec='milliseconds')
            lines += [f'#EXT-X-PROGRAM-DATE-TIME:{date}', f'#EXTINF:{entry.duration:.6f},',
                      f'{base}/segments/{entry.asset}/{entry.segment}']
        return '\n'.join(lines) + '\n'
