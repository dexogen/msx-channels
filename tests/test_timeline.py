from pathlib import Path

from msx_channels.media import Asset
from msx_channels.store import Store
from msx_channels.timeline import Timeline


def asset(key, durations=(2.0, 2.0)):
    return Asset(key, Path('/tmp') / key, tuple((d, f'segment-{n:06d}.ts') for n, d in enumerate(durations)))


def test_switch_preserves_published_segments_and_sequences():
    first, second = asset('a'), asset('b', (2.0, 1.3))
    timeline = Timeline(first, now=100)
    published = list(timeline.entries)
    timeline.select(second)
    timeline.tick(101)
    assert list(timeline.entries)[:len(published)] == published
    assert timeline.entries[-1].asset == 'b'
    assert timeline.entries[-1].boundary
    sequences = [e.sequence for e in timeline.entries]
    assert sequences == list(range(sequences[0], sequences[-1] + 1))
    assert '#EXT-X-ENDLIST' not in timeline.manifest('http://test')


def test_discontinuity_sequence_survives_sliding_window():
    timeline = Timeline(asset('a', (0.25,)), now=100)
    for now in range(101, 200):
        timeline.tick(now)
    first = timeline.entries[0]
    manifest = timeline.manifest('http://test')
    assert f'#EXT-X-DISCONTINUITY-SEQUENCE:{first.discontinuity - 1}' in manifest
    assert first.start >= 178
    assert len(timeline.entries) < 105


def test_channel_identity_persists_across_edit_and_restart(tmp_path):
    path = tmp_path / 'channels.db'
    store = Store(path)
    identifier = store.save('Channel', 'old.mp4', 0)
    assert store.save('New title', 'new.mov', 90, identifier) == identifier
    store.close()
    store = Store(path)
    assert store.all()[0]['id'] == identifier
    assert store.all()[0]['video'] == 'new.mov'
    store.delete(identifier)
    assert store.all() == []
    store.close()
