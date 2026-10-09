"""Offline suite: never select a paid model from a developer's .env file."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ['LLM_PROVIDER'] = 'mock'
os.environ['PRODUCT_SOURCE'] = 'json'
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'


os.environ.update({'CHECKPOINTER_BACKEND': 'memory', 'GREENY_AI_MONGO_URL': '',
                   'ENVIRONMENT': 'development', 'KIOSK_AUTH_SECRET': '',
                   'KIOSK_GATEWAY_KEY': '', 'ALLOWED_KIOSK_IDS': '*'})

def pytest_addoption(parser):
    parser.addoption('--run-live', action='store_true', default=False)

def pytest_configure(config):
    config.addinivalue_line('markers', 'live: read-only external integration check (opt in)')

def pytest_collection_modifyitems(config, items):
    import pytest
    for item in items:
        if 'greenfibre_live_repo_schema' in item.name:
            item.add_marker(pytest.mark.live)
        if item.get_closest_marker('live') and not config.getoption('--run-live'):
            item.add_marker(pytest.mark.skip(reason='External check: requires --run-live'))

# Deterministic tests never download embedding models or open external sockets.
# RAG-specific tests inject their own stores/embeddings after this fixture.
import pytest

@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, request):
    if request.node.get_closest_marker('live'):
        return
    import socket
    original_connect = socket.socket.connect
    def offline_connect(sock, address):
        if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1', 'localhost'):
            raise RuntimeError('External networking disabled in deterministic tests')
        return original_connect(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', offline_connect)
    from app import server, brain
    monkeypatch.setattr(server, 'warmup_rag', lambda: {'total_warmup_ms': 0, 'embeddings_load_ms': 0, 'vector_store_load_ms': 0})
    monkeypatch.setattr(brain, 'retrieve_relevant_chunks', lambda *a, **kw: [])
    from app.coordination import local_coordinator
    local_coordinator.rates.clear()
