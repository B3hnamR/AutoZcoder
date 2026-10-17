"""AES-256-GCM using Windows CNG, so the portable app needs no crypto package."""
import ctypes as c
import sys


def decrypt_aes_gcm(key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes) -> bytes:
    if len(key) != 32 or len(nonce) != 12 or len(tag) != 16:
        raise ValueError('Invalid ZCode credential cipher parameters')
    if sys.platform != 'win32':
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(key).decrypt(nonce, ciphertext + tag, None)
    from ctypes import wintypes as w
    dll = c.WinDLL('bcrypt.dll')
    ptr, ulong = c.c_void_p, w.ULONG

    class AuthInfo(c.Structure):
        _fields_ = [('cbSize', ulong), ('dwInfoVersion', ulong), ('pbNonce', ptr),
                    ('cbNonce', ulong), ('pbAuthData', ptr), ('cbAuthData', ulong),
                    ('pbTag', ptr), ('cbTag', ulong), ('pbMacContext', ptr),
                    ('cbMacContext', ulong), ('cbAAD', ulong),
                    ('cbData', c.c_ulonglong), ('dwFlags', ulong)]

    signatures = {
        'BCryptOpenAlgorithmProvider': [c.POINTER(ptr), w.LPCWSTR, w.LPCWSTR, ulong],
        'BCryptSetProperty': [ptr, w.LPCWSTR, ptr, ulong, ulong],
        'BCryptGetProperty': [ptr, w.LPCWSTR, ptr, ulong, c.POINTER(ulong), ulong],
        'BCryptGenerateSymmetricKey': [ptr, c.POINTER(ptr), ptr, ulong, ptr, ulong, ulong],
        'BCryptDecrypt': [ptr, ptr, ulong, ptr, ptr, ulong, ptr, ulong, c.POINTER(ulong), ulong],
        'BCryptDestroyKey': [ptr], 'BCryptCloseAlgorithmProvider': [ptr, ulong],
    }
    for name, args in signatures.items():
        function = getattr(dll, name)
        function.argtypes, function.restype = args, w.LONG

    def checked(status):
        if status < 0:
            raise ValueError('ZCode credential authentication failed')

    algorithm, handle = ptr(), ptr()
    checked(dll.BCryptOpenAlgorithmProvider(c.byref(algorithm), 'AES', None, 0))
    try:
        mode = c.create_unicode_buffer('ChainingModeGCM')
        checked(dll.BCryptSetProperty(algorithm, 'ChainingMode', mode, c.sizeof(mode), 0))
        length, read = ulong(), ulong()
        checked(dll.BCryptGetProperty(algorithm, 'ObjectLength', c.byref(length), c.sizeof(length), c.byref(read), 0))
        obj, key_buf = c.create_string_buffer(length.value), c.create_string_buffer(key)
        checked(dll.BCryptGenerateSymmetricKey(algorithm, c.byref(handle), obj, length, key_buf, len(key), 0))
        iv, auth_tag, encrypted = c.create_string_buffer(nonce), c.create_string_buffer(tag), c.create_string_buffer(ciphertext)
        info = AuthInfo()
        info.cbSize, info.dwInfoVersion = c.sizeof(info), 1
        info.pbNonce, info.cbNonce = c.cast(iv, ptr), len(nonce)
        info.pbTag, info.cbTag = c.cast(auth_tag, ptr), len(tag)
        output, written = c.create_string_buffer(max(1, len(ciphertext))), ulong()
        checked(dll.BCryptDecrypt(handle, encrypted, len(ciphertext), c.byref(info), None, 0,
                                 output, len(ciphertext), c.byref(written), 0))
        return output.raw[:written.value]
    finally:
        if handle: dll.BCryptDestroyKey(handle)
        dll.BCryptCloseAlgorithmProvider(algorithm, 0)
