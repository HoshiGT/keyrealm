"""通用 SMTP 邮箱验证插件（plugins/mail/ 类别下的独立子插件）。"""
from mail_common import display_summary, validate_common, verify_service

SERVICE = "smtp"


def validate(data):
    return validate_common(SERVICE, data)


def verify(data):
    return verify_service(SERVICE, data)


def get_display_summary(data):
    return display_summary(SERVICE, data)
