"""The nftables set `inet filter fenced` that drops a fenced worker's bridge address."""
import ipaddress
import json

from . import host

SET = ['inet', 'filter', 'fenced']


def _addr(addr):
    return str(ipaddress.IPv4Address(addr))  # raises ValueError; keeps nft syntax out of the argv


def _elements():
    for item in json.loads(host.run(['nft', '-j', 'list', 'set', *SET]).stdout)['nftables']:
        if 'set' in item:
            for e in item['set'].get('elem', []):
                if isinstance(e, str):
                    yield e
                elif isinstance(e, dict) and isinstance(e.get('elem'), dict):
                    yield e['elem'].get('val')


def is_fenced(addr):
    return _addr(addr) in set(_elements())


def ensure_fenced(addr):
    host.run(['nft', 'add', 'element', *SET, '{ %s }' % _addr(addr)])  # add is idempotent


def remove_fenced(addr):
    if is_fenced(addr):
        host.run(['nft', 'delete', 'element', *SET, '{ %s }' % _addr(addr)])
