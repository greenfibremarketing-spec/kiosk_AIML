"""One validated command contract for existing REST and WebSocket transports."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.config import settings


class GuidedCommandError(ValueError):
    """Valid JSON, but invalid command for this conversation state."""


class AskNameAction(BaseModel):
    """Backend-to-frontend action; never an input command."""
    model_config = ConfigDict(extra='forbid')
    action: Literal['ASK_NAME']
    prompt: str = Field(min_length=1, max_length=200)
    skip_allowed: Literal[True]
    message: str | None = Field(default=None, max_length=200)
    options: list[dict[str, str]] | None = None


def validate_screen_action(action):
    if isinstance(action, dict) and action.get('action') == 'ASK_NAME':
        return AskNameAction.model_validate(action).model_dump(exclude_none=True)
    return action


class ConversationInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    message: str | None = Field(default=None, min_length=1, max_length=settings.max_message_chars)
    action: Literal['SPEECH_DONE', 'SET_NAME', 'SKIP_NAME', 'SELECT_CATEGORY',
                    'SHOW_ALL', 'SELECT_PRODUCT', 'NAVIGATE_BACK', 'RESTART'] | None = None
    turn_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{1,64}$')
    speech_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    name: str | None = Field(default=None, min_length=1, max_length=60)
    category_id: str | None = Field(default=None, min_length=1, max_length=100)
    sku: str | None = Field(default=None, max_length=128, pattern=r'^GF:[A-Za-z0-9_-]{1,128}(?::[A-Za-z0-9_-]{1,128})?$')
    input_mode: Literal['text', 'voice', 'button'] = 'text'

    @model_validator(mode='after')
    def validate_command(self):
        fields = {'speech_id', 'name', 'category_id', 'sku'}
        if bool(self.message) == bool(self.action):
            raise ValueError('Provide either message or action.')
        expected = {'SPEECH_DONE': 'speech_id', 'SET_NAME': 'name',
                    'SELECT_CATEGORY': 'category_id', 'SELECT_PRODUCT': 'sku'}.get(self.action)
        if self.action and not self.turn_id:
            raise ValueError('Commands require turn_id.')
        if expected and not getattr(self, expected):
            raise ValueError('Missing command value.')
        if any(getattr(self, field) is not None for field in fields if field != expected):
            raise ValueError('Unexpected command value.')
        if self.name and (not any(c.isalpha() for c in self.name) or
                          any(not (c.isalpha() or c in " '-") for c in self.name)):
            raise ValueError('Name must contain letters, spaces, apostrophes or hyphens only.')
        return self

    def normalized_message(self):
        if self.message:
            return self.message
        return {
            'SPEECH_DONE': 'SPEECH_DONE', 'SET_NAME': self.name or '',
            'SKIP_NAME': 'SKIP_NAME', 'SELECT_CATEGORY': f'select_category:{self.category_id}',
            'SHOW_ALL': 'show all', 'SELECT_PRODUCT': f'select_product:{self.sku}',
            'NAVIGATE_BACK': 'back', 'RESTART': 'restart',
        }[self.action]

    def command(self):
        return self.model_dump(exclude_none=True) if self.action else None
