"""第三方中转 (Anthropic 协议) API Key 验证插件（plugins/llm/ 类别下的独立子插件）。"""
from llm_common import display_summary, validate_common, verify_vendor

VENDOR = "anthropic_compatible"


def validate(data):
    return validate_common(VENDOR, data)


def verify(data):
    return verify_vendor(VENDOR, data)


def get_display_summary(data):
    return display_summary(VENDOR, data)
