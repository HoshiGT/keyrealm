"""Cloudflare API Token / Global API Key 验证插件（plugins/cloud/ 类别下的独立子插件）。"""
from cloud_common import display_summary, validate_common, verify_service

SERVICE = "cloudflare"


def validate(data):
    return validate_common(SERVICE, data)


def verify(data):
    return verify_service(SERVICE, data)


def get_display_summary(data):
    return display_summary(SERVICE, data)
