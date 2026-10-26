import json

from bluehead.quality_log import AudioCounters


def test_counters_are_opt_in_numeric_bounded_and_throttled(tmp_path, monkeypatch):
    now = [0]
    record = {'continuity': {'sources': [{'queued_frames': 123, 'media': {'codec': 2},
                                         'name': 'private device', 'pcm': b'private audio'}],
                             'voice': {'active': True}, 'address': 'private', 'key': 'private'}}
    disabled = AudioCounters(tmp_path)
    disabled.record(record)
    assert not list(tmp_path.iterdir())
    monkeypatch.setenv('BLUEHEAD_AUDIO_COUNTER_LOG', '1')
    journal = AudioCounters(tmp_path, clock=lambda: now[0], maximum=180)
    journal.record(record)
    journal.record(record)
    assert len(journal.path.read_text('utf-8').splitlines()) == 1
    now[0] = 3
    journal.record(record)
    assert journal.path.with_suffix('.log.1').exists()
    for path in tmp_path.iterdir():
        text = path.read_text('utf-8')
        assert 'private' not in text and 'address' not in text and 'pcm' not in text
        data = json.loads(text)
        assert data['sources'][0]['queued_frames'] == 123


def test_counter_write_failure_disables_logging_without_breaking_control(tmp_path, monkeypatch):
    monkeypatch.setenv('BLUEHEAD_AUDIO_COUNTER_LOG', '1')
    blocked = tmp_path / 'not-a-directory'
    blocked.write_text('x', 'utf-8')
    journal = AudioCounters(blocked)
    journal.record({'continuity': {'usb': {'active': False}}})
    assert journal.enabled is False
