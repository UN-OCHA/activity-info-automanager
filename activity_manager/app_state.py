"""Local application preferences; tokens are protected for the Windows user."""
import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import tempfile


class _Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data: bytes, *, decrypt=False) -> bytes:
    if os.name != 'nt':
        raise OSError('DPAPI requires Windows.')
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError('Windows could not decrypt the saved token.' if decrypt
                      else 'Windows could not protect the API token.')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel32.LocalFree(target.data)


class AppStateStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else (
            Path(os.environ['LOCALAPPDATA']) / 'ActivityInfoManager' / 'app-state.json')

    def load(self):
        """Return preferences and an optional token warning without losing commands."""
        if not self.path.exists():
            return None, ''
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('version') != 1:
            raise ValueError('Unsupported application state file.')
        for key in ('dbid', 'formid'):
            if data.get(key) is not None and not isinstance(data[key], str):
                raise ValueError('Invalid saved selection.')
        if not isinstance(data.get('commands', ''), str):
            raise ValueError('Invalid saved commands.')
        warning = ''
        try:
            encrypted = data.get('api_token_dpapi', '')
            data['api_token'] = _crypt(base64.b64decode(encrypted, validate=True), decrypt=True).decode('utf-8') if encrypted else ''
        except (OSError, ValueError, TypeError):
            data['api_token'] = ''
            warning = 'Saved API token could not be decrypted. Enter it again.'
        return data, warning

    def save(self, *, api_token, dbid, formid, commands):
        data = dict(version=1, api_token_dpapi=(base64.b64encode(_crypt(api_token.encode('utf-8'))).decode('ascii') if api_token else ''),
                    dbid=dbid, formid=formid, commands=commands)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent, delete=False) as output:
                name = output.name
                json.dump(data, output, ensure_ascii=False, indent=2)
            os.replace(name, self.path)
        finally:
            if name and Path(name).exists():
                Path(name).unlink()
