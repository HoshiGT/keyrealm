"""HuggingFace 访问令牌验证插件（plugins/dev/ 类别下的独立子插件）。"""
from dev_common import display_summary, validate_common, verify_service

SERVICE = "huggingface"


def validate(data):
    return validate_common(SERVICE, data)


def verify(data):
    return verify_service(SERVICE, data)


def get_display_summary(data):
    return display_summary(SERVICE, data)
