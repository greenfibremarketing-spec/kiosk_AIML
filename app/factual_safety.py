"""Fail-closed compliance boundary until an approved SKU evidence store exists."""
import re

COMPLIANCE = re.compile(
    r'\bsafe\b|\bISO\b|\bFDA\b|\bFSSAI\b|\bBIS\b|compliant|certif|\bBPA\b|food[\s-]?(?:safe|grade)|carbon[\s-]?negative|non[\s-]?toxic|'
    r'(?:dishwasher|microwave)[\s-]?safe|safe for (?:food|children)|'
    r'(?:safe|suitable) (?:in|for) (?:the )?(?:microwave|dishwasher)|'
    r'प्रमाणित|प्रमाणन|सर्टिफ|खाद्य सुरक्षित', re.I,
)


def safe_description(text):
    """A merchant description is not a compliance approval record."""
    return '' if COMPLIANCE.search(str(text)) else text
