from unittest.mock import Mock
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app import server
from app.config import settings


@pytest.fixture
def intro_client(monkeypatch):
    monkeypatch.setattr(settings, 'kiosk_auth_secret', None)
    monkeypatch.setattr(settings, 'environment', 'development')
    async def no_warmup():
        return None
    monkeypatch.setattr(server, 'warmup_conversation_agent', no_warmup)
    greeting = Mock(return_value=('Hello from Greeny.', {'action': 'SHOW_GREETING', 'speech_id': 'speech-test'}, None))
    monkeypatch.setattr(server, 'fast_transition', greeting)
    with TestClient(server.api) as client:
        yield client, greeting


@pytest.mark.parametrize('prefix', ['', '/v1'])
def test_public_playlist_contains_all_supplied_videos_in_order(intro_client, prefix):
    client, _ = intro_client
    response = client.get(prefix + '/media/intro-videos')
    assert response.status_code == 200
    data = response.json()
    assert [video['id'] for video in data['videos']] == ['kiosk-1', 'kiosk-3', 'kiosk-5', 'kiosk-4', 'kiosk-2']
    assert len({video['url'] for video in data['videos']}) == 5
    assert all(video['url'].startswith('https://res.cloudinary.com/dsebrpcyz/video/upload/') and video['url'].endswith('.mp4') for video in data['videos'])
    assert data['playback']['muted'] is True
    assert data['playback']['loop'] is False
    assert data['next_action'] == 'SHOW_GREETING'


def test_player_module_is_served(intro_client):
    response = intro_client[0].get('/media/intro-player.js')
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/javascript')
    assert 'export function playIntroPlaylist' in response.text


def test_demo_page_is_served(intro_client):
    response = intro_client[0].get('/media/demo')
    assert response.status_code == 200
    assert 'Welcome Intro Videos Player' in response.text


@pytest.mark.parametrize('event', ['intro_done', 'intro_skipped', 'intro_failed'])
def test_greeny_waits_for_intro_completion_and_greets_once(intro_client, event):
    client, greeting = intro_client
    session = 'intro-' + uuid4().hex
    with client.websocket_connect(f'/ws/{session}?v=2&auto_greet=1&intro=1') as ws:
        first = ws.receive_json()
        assert first['type'] == 'action'
        assert first['data']['action'] == 'SHOW_INTRO_VIDEOS'
        assert len(first['data']['videos']) == 5
        greeting.assert_not_called()
        ws.send_json({'type': 'ping'})
        assert ws.receive_json()['type'] == 'pong'
        greeting.assert_not_called()
        ws.send_json({'type': event})
        frames = []
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame['type'] == 'done':
                break
        assert frames[0]['data']['action'] == 'SHOW_GREETING'
        greeting.assert_called_once_with(session, 'hello')
        # Duplicate browser events must not restart Greeny's greeting.
        ws.send_json({'type': event})
        ws.send_json({'type': 'ping'})
        assert ws.receive_json()['type'] == 'pong'
        assert greeting.call_count == 1


def test_chat_is_gated_while_intro_plays(intro_client):
    client, greeting = intro_client
    with client.websocket_connect('/ws/intro-gated?v=2&auto_greet=1&intro=1') as ws:
        assert ws.receive_json()['data']['action'] == 'SHOW_INTRO_VIDEOS'
        ws.send_json({'message': 'hello'})
        response = ws.receive_json()
        assert response['type'] == 'error'
        assert response['meta']['code'] == 'intro_in_progress'
        greeting.assert_not_called()


def test_existing_frontend_keeps_immediate_greeting(intro_client):
    client, greeting = intro_client
    with client.websocket_connect('/ws/intro-legacy?v=2&auto_greet=1') as ws:
        assert ws.receive_json()['data']['action'] == 'SHOW_GREETING'
        greeting.assert_called_once()


def test_reconnect_with_auto_greet_disabled_does_not_replay_intro(intro_client):
    client, greeting = intro_client
    with client.websocket_connect('/ws/intro-reconnect?v=2&auto_greet=0&intro=1') as ws:
        ws.send_json({'type': 'ping'})
        assert ws.receive_json()['type'] == 'pong'
        greeting.assert_not_called()
