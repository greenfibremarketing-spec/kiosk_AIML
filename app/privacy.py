"""Contact minimization before model, checkpoint, operational storage and traces."""
import re
from langchain_core.messages import BaseMessage

_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\w])(?:\+\d{1,3}[ .-]?)?(?:\(?\d\)?[ .-]?){10,13}(?![\w])")


def redact_contacts(value):
    if isinstance(value, str):
        return _PHONE.sub('[CONTACT REDACTED]', _EMAIL.sub('[CONTACT REDACTED]', value))
    if isinstance(value, BaseMessage):
        return value.model_copy(update={
            key: redact_contacts(val) for key, val in value.model_dump().items()
            if key in ('content', 'tool_calls', 'additional_kwargs', 'response_metadata')
        })
    if isinstance(value, dict):
        return {key: redact_contacts(val) for key, val in value.items()}
    if isinstance(value, list):
        return [redact_contacts(val) for val in value]
    if isinstance(value, tuple):
        return tuple(redact_contacts(val) for val in value)
    return value
