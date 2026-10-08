from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from app import server


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, 'warmup_rag', lambda: {
        'total_warmup_ms': 0, 'embeddings_load_ms': 0, 'vector_store_load_ms': 0,
    })
    monkeypatch.setattr(server, 'ask_avatar', Mock(return_value='Hello'))
    monkeypatch.setattr(server, 'ask_avatar_stream', Mock(return_value=iter(['Hello'])))
    with TestClient(server.api) as client:
        yield client


def test_chat_contract_and_secure_generated_identifier(client):
    response = client.post('/chat', json={'message': 'hello'})
    assert len(response.headers['X-Request-ID']) == 32
    reply = response.json()
    assert set(reply) == {'reply', 'session_id'}
    assert reply['reply'] == 'Hello'
    assert len(reply['session_id']) == 36


@pytest.mark.parametrize('message', ['नमस्ते', 'Mujhe gift chahiye'])
def test_unicode_input_reaches_agent_unchanged(client, message):
    assert client.post('/chat', json={'message': message}).status_code == 200
    assert server.ask_avatar.call_args.kwargs['message'] == message


@pytest.mark.parametrize('endpoint,attribute,payload', [
    ('/chat', 'ask_avatar', {'message': 'hello'}),
    ('/products', '_load_catalog', None),
])
def test_public_errors_hide_exception_and_logs_hide_payload(client, monkeypatch, caplog, endpoint, attribute, payload):
    monkeypatch.setattr(server, attribute, Mock(side_effect=RuntimeError('SECRET password person@example.test')))
    response = client.get(endpoint) if payload is None else client.post(endpoint, json=payload)
    assert response.status_code == 500
    assert 'SECRET' not in response.text + caplog.text
    assert 'person@example' not in response.text + caplog.text


def test_reset_failure_is_not_reported_as_success(client, monkeypatch):
    monkeypatch.setattr(server.session_manager, 'reset_session', Mock(side_effect=RuntimeError('SECRET')))
    response = client.post('/session/reset', json={'session_id': 'one'})
    assert response.status_code == 503
    assert 'SECRET' not in response.text


def test_validation_does_not_echo_private_input(client):
    message = 'PRIVATE' * 1000
    response = client.post('/chat', json={'message': message})
    assert response.status_code == 422
    assert 'PRIVATE' not in response.text
    server.ask_avatar.assert_not_called()


def test_ws_invalid_payload_recovers_and_retains_events(client):
    with client.websocket_connect('/ws/one') as ws:
        ws.send_json({'message': 12})
        assert ws.receive_json()['type'] == 'error'
        ws.send_json({'message': 'hello'})
        assert ws.receive_json() == {'type': 'token', 'data': 'Hello'}
        assert ws.receive_json() == {'type': 'done', 'data': 'Hello'}


def test_ws_failure_is_safe_and_does_not_repeat_turn(client, monkeypatch, caplog):
    broken = Mock(side_effect=RuntimeError('SECRET contact@example.test'))
    monkeypatch.setattr(server, 'ask_avatar_stream', broken)
    with client.websocket_connect('/ws/one') as ws:
        ws.send_text('hello')
        reply = ws.receive_json()
        assert reply['type'] == 'error'
        assert 'SECRET' not in str(reply) + caplog.text
    assert broken.call_count == 1
    server.ask_avatar.assert_not_called()


def test_ws_rejects_oversize_message_before_inference(client):
    with client.websocket_connect('/ws/one') as ws:
        ws.send_text('x' * (server.settings.max_message_chars + 1))
        assert ws.receive_json()['type'] == 'error'
    server.ask_avatar_stream.assert_not_called()
