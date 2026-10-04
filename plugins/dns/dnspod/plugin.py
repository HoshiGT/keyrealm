"""DNSPod API Token 验证插件（plugins/dns/ 类别下的独立子插件）。"""
from dns_common import display_summary, validate_common, verify_service

SERVICE = "dnspod"


def validate(data):
    return validate_common(SERVICE, data)


def verify(data):
    return verify_service(SERVICE, data)


def get_display_summary(data):
    return display_summary(SERVICE, data)
