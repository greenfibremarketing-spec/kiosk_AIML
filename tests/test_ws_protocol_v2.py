"""Tests for WebSocket Protocol v2, versioned endpoints, and stream control."""

from unittest.mock import Mock
import pytest
from starlette.testclient import TestClient

from app import server


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, 'ask_avatar', Mock(return_value='Hello'))
    monkeypatch.setattr(server, 'ask_avatar_stream', Mock(return_value=iter(['Hello', ' world.', ' How', ' are', ' you?'])))
    with TestClient(server.api) as client:
        yield client


def test_ws_ping_pong(client):
    with client.websocket_connect('/ws/sess-ping') as ws:
        ws.send_json({'type': 'ping'})
        reply = ws.receive_json()
        assert reply['type'] == 'pong'
        assert reply['data'] == ''


def test_ws_protocol_v2_envelopes_and_sentence_events(client):
    with client.websocket_connect('/ws/sess-v2?v=2') as ws:
        ws.send_json({'message': 'Tell me about bottles.'})
        events = []
        while True:
            ev = ws.receive_json()
            events.append(ev)
            if ev.get('type') == 'done':
                break

        # Check that all envelopes have v: 2
        for ev in events:
            assert ev['v'] == 2

        types = [e['type'] for e in events]
        assert 'token' in types
        assert 'sentence' in types
        assert 'done' in types

        # Check sentence boundaries were recognized
        sentences = [e['data'] for e in events if e['type'] == 'sentence']
        assert 'Hello world.' in sentences

        # Check done event includes metadata
        done_ev = [e for e in events if e['type'] == 'done'][0]
        assert done_ev['meta']['session_id'] == 'sess-v2'


def test_ws_turn_cancellation(client, monkeypatch):
    # Simulate a generator that yields several tokens and sets cancel flag
    def _slow_stream(message, session_id, kiosk_id=None):
        yield "Part 1. "
        server._cancel_flags[session_id] = True
        yield "Part 2. "

    monkeypatch.setattr(server, 'ask_avatar_stream', _slow_stream)
    with client.websocket_connect('/ws/sess-cancel?v=2') as ws:
        ws.send_json({'message': 'Hello'})
        events = []
        while True:
            ev = ws.receive_json()
            events.append(ev)
            if ev.get('type') in ('done', 'cancelled', 'error'):
                break

        assert any(e['type'] == 'cancelled' for e in events)


def test_versioned_rest_aliases(client):
    # Test POST /v1/chat
    res = client.post('/v1/chat', json={'message': 'hello'})
    assert res.status_code == 200
    data = res.json()
    assert 'reply' in data
    assert 'session_id' in data

    # Test POST /v1/session/reset
    res_reset = client.post('/v1/session/reset', json={'session_id': data['session_id']})
    assert res_reset.status_code == 200
    assert res_reset.json()['status'] == 'ok'
