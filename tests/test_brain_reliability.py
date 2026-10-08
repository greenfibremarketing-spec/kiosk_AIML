import json
from app.brain import MockKioskChatModel, validate_reply_factual_numbers
from app import brain
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from unittest.mock import Mock


def test_mock_does_not_invent_price_or_stock():
    reply = MockKioskChatModel()._synthesize_tool_reply(json.dumps({'name': 'Cup', 'price': None, 'stock': None}))
    assert '0 rupees' not in reply
    assert 'available' not in reply
    assert 'confirmation' in reply


def test_mock_preserves_decimal_price():
    reply = MockKioskChatModel()._synthesize_tool_reply(json.dumps([{'name': 'Cup', 'price': 123.45}]))
    assert '123.45 rupees' in reply


def test_small_prices_and_stock_need_evidence():
    assert not validate_reply_factual_numbers('It costs 2 rupees.', [], [])[0]
    assert not validate_reply_factual_numbers('2 units available.', [], [])[0]


def test_material_mock_does_not_make_blanket_claim():
    reply = MockKioskChatModel()._generate_rag_reply('What material do you use?')
    assert '100%' not in reply
    assert 'verified' in reply


def test_old_tool_numbers_cannot_validate_current_turn(monkeypatch):
    monkeypatch.setattr(brain.kiosk_brain, 'invoke', Mock(return_value={'messages': [
        HumanMessage(content='old question'),
        ToolMessage(content='price: 499', tool_call_id='old'),
        HumanMessage(content='new question'), AIMessage(content='It costs 499 rupees.'),
    ]}))
    monkeypatch.setattr(brain, 'get_llm', Mock(side_effect=RuntimeError('offline')))
    assert brain.ask_avatar('new question', 'test-turn-evidence') == brain.FALLBACK_VALIDATION_REPLY


def test_real_graph_offline_product_turn(monkeypatch):
    monkeypatch.setattr(brain, 'retrieve_relevant_chunks', lambda *args, **kwargs: [])
    reply = brain.ask_avatar('What is the price of a mug?', 'offline-product-test')
    assert 'catalogue price' in reply.lower()
    assert 'confirm' in reply.lower()
