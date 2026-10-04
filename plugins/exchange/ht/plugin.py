"""华泰证券（涨乐）凭证插件（plugins/exchange/ 类别下的独立子插件）。

注意：华泰证券未开放公开的联网账户校验接口，verify 仅返回占位提示，
不做任何网络请求，详见 exchange_common._verify_ht 的说明。
"""
from exchange_common import display_summary, validate_common, verify_service

SERVICE = "ht"


def validate(data):
    return validate_common(SERVICE, data)


def verify(data):
    return verify_service(SERVICE, data)


def get_display_summary(data):
    return display_summary(SERVICE, data)
