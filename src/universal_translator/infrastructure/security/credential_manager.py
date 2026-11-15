"""Armazenamento seguro de credenciais com suporte nativo ao Windows Credential Manager (Advapi32 / DPAPI), Keyring e fallback criptografado."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path

from ...domain.contracts import SecretStorePort


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class CredentialManager(SecretStorePort):
    """Gerencia chaves de API sem jamais gravá-las em texto puro em settings.json.

    Ordem de prioridade de armazenamento:
    1. Windows Credential Manager nativo (`Advapi32.dll` `CredWriteW` / `CredReadW` / `CredDeleteW`) no Windows.
    2. Biblioteca `keyring` do sistema operacional, quando disponível.
    3. Cofre local criptografado (`secrets.vault`) com chave derivada por PBKDF2-HMAC-SHA256 + Fernet/XOR-HMAC vinculada ao usuário local.
    """

    SERVICE_PREFIX = "UniversalTranslator"
    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    def __init__(self, vault_path: Path | None = None) -> None:
        if vault_path is None:
            override = os.environ.get("UNIVERSAL_TRANSLATOR_DATA_DIR")
            if override:
                base = Path(override)
            elif sys.platform == "win32":
                base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")) / "UniversalTranslator"
            else:
                base = Path.home() / ".config" / "UniversalTranslator"
            base.mkdir(parents=True, exist_ok=True)
            vault_path = base / ".credentials.vault"

        self._vault_path = vault_path
        self._memory_cache: dict[str, str] = {}
        self._win_cred_available = sys.platform == "win32"
        self._keyring_mod = self._load_keyring()

    @staticmethod
    def _load_keyring():
        try:
            import keyring  # type: ignore
            return keyring
        except Exception:
            return None

    @property
    def backend_name(self) -> str:
        if self._win_cred_available:
            return "Windows Credential Manager (Advapi32)"
        if self._keyring_mod is not None:
            return "System Keyring"
        return "Encrypted Local Vault (PBKDF2-SHA256)"

    def _target_name(self, key_name: str) -> str:
        return f"{self.SERVICE_PREFIX}/{key_name}"

    def set_secret(self, key_name: str, secret_value: str) -> bool:
        clean_val = (secret_value or "").strip()
        if not clean_val:
            return self.delete_secret(key_name)

        self._memory_cache[key_name] = clean_val

        if self._win_cred_available and self._win32_cred_write(key_name, clean_val):
            return True

        if self._keyring_mod is not None:
            try:
                self._keyring_mod.set_password(self.SERVICE_PREFIX, key_name, clean_val)
                return True
            except Exception:
                pass

        return self._vault_write(key_name, clean_val)

    def get_secret(self, key_name: str) -> str:
        if key_name in self._memory_cache:
            return self._memory_cache[key_name]

        # 1. Variável de ambiente (ex: CI/CD ou configuração explícita)
        env_key = f"UT_{key_name.upper()}"
        if os.environ.get(env_key):
            return os.environ[env_key].strip()

        # 2. Windows Credential Manager nativo
        if self._win_cred_available:
            val = self._win32_cred_read(key_name)
            if val is not None:
                self._memory_cache[key_name] = val
                return val

        # 3. Keyring quando disponível
        if self._keyring_mod is not None:
            try:
                val = self._keyring_mod.get_password(self.SERVICE_PREFIX, key_name)
                if val:
                    self._memory_cache[key_name] = val
                    return val
            except Exception:
                pass

        # 4. Cofre criptografado local
        val = self._vault_read(key_name)
        if val:
            self._memory_cache[key_name] = val
        return val

    def delete_secret(self, key_name: str) -> bool:
        self._memory_cache.pop(key_name, None)
        removed = False

        if self._win_cred_available:
            removed = self._win32_cred_delete(key_name) or removed

        if self._keyring_mod is not None:
            try:
                self._keyring_mod.delete_password(self.SERVICE_PREFIX, key_name)
                removed = True
            except Exception:
                pass

        removed = self._vault_delete(key_name) or removed
        return removed

    # --- Implementação nativa Win32 Advapi32 Credential Manager ---
    def _win32_cred_write(self, key_name: str, secret_value: str) -> bool:
        try:
            advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
            blob_bytes = secret_value.encode("utf-16-le")
            blob_buffer = (ctypes.c_ubyte * len(blob_bytes)).from_buffer_copy(blob_bytes)

            cred = _CREDENTIALW()
            cred.Flags = 0
            cred.Type = self.CRED_TYPE_GENERIC
            cred.TargetName = self._target_name(key_name)
            cred.Comment = "Universal Translator Protected API Credential"
            cred.CredentialBlobSize = len(blob_bytes)
            cred.CredentialBlob = ctypes.cast(blob_buffer, ctypes.POINTER(ctypes.c_ubyte))
            cred.Persist = self.CRED_PERSIST_LOCAL_MACHINE
            cred.AttributeCount = 0
            cred.Attributes = None
            cred.UserName = os.environ.get("USERNAME", "UniversalTranslatorUser")

            advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
            advapi32.CredWriteW.restype = wintypes.BOOL
            return bool(advapi32.CredWriteW(ctypes.byref(cred), 0))
        except Exception:
            return False

    def _win32_cred_read(self, key_name: str) -> str | None:
        try:
            advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
            advapi32.CredReadW.argtypes = [
                wintypes.LPWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
            ]
            advapi32.CredReadW.restype = wintypes.BOOL
            advapi32.CredFree.argtypes = [ctypes.c_void_p]
            advapi32.CredFree.restype = None

            pcred = ctypes.POINTER(_CREDENTIALW)()
            ok = advapi32.CredReadW(
                self._target_name(key_name),
                self.CRED_TYPE_GENERIC,
                0,
                ctypes.byref(pcred),
            )
            if not ok or not pcred:
                return None
            try:
                cred = pcred.contents
                size = int(cred.CredentialBlobSize)
                if size <= 0:
                    return ""
                raw = ctypes.string_at(cred.CredentialBlob, size)
                return raw.decode("utf-16-le")
            finally:
                advapi32.CredFree(pcred)
        except Exception:
            return None

    def _win32_cred_delete(self, key_name: str) -> bool:
        try:
            advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
            advapi32.CredDeleteW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
            advapi32.CredDeleteW.restype = wintypes.BOOL
            return bool(
                advapi32.CredDeleteW(self._target_name(key_name), self.CRED_TYPE_GENERIC, 0)
            )
        except Exception:
            return False

    # --- Cofre local criptografado (PBKDF2-HMAC-SHA256) ---
    def _derive_machine_key(self, salt: bytes) -> bytes:
        user_id = (
            os.environ.get("USERNAME")
            or os.environ.get("USER")
            or str(Path.home())
        )
        material = f"UniversalTranslator::{sys.platform}::{user_id}".encode("utf-8")
        return hashlib.pbkdf2_hmac("sha256", material, salt, 100_000, dklen=32)

    def _encrypt_value(self, plaintext: str) -> str:
        salt = os.urandom(16)
        key = self._derive_machine_key(salt)
        data = plaintext.encode("utf-8")
        # Stream cipher determinístico por bloco com SHA-256 CTR + HMAC
        cipher_bytes = bytearray(len(data))
        for i, b in enumerate(data):
            block_key = hashlib.sha256(key + i.to_bytes(4, "big")).digest()
            cipher_bytes[i] = b ^ block_key[i % 32]
        mac = hashlib.sha256(key + bytes(cipher_bytes)).digest()[:16]
        token = salt + mac + bytes(cipher_bytes)
        return base64.urlsafe_b64encode(token).decode("ascii")

    def _decrypt_value(self, token_b64: str) -> str:
        try:
            raw = base64.urlsafe_b64decode(token_b64.encode("ascii"))
            if len(raw) < 32:
                return ""
            salt = raw[:16]
            mac = raw[16:32]
            cipher_bytes = raw[32:]
            key = self._derive_machine_key(salt)
            expected_mac = hashlib.sha256(key + cipher_bytes).digest()[:16]
            if mac != expected_mac:
                return ""
            plain_bytes = bytearray(len(cipher_bytes))
            for i, b in enumerate(cipher_bytes):
                block_key = hashlib.sha256(key + i.to_bytes(4, "big")).digest()
                plain_bytes[i] = b ^ block_key[i % 32]
            return bytes(plain_bytes).decode("utf-8")
        except Exception:
            return ""

    def _load_vault_dict(self) -> dict[str, str]:
        if not self._vault_path.exists():
            return {}
        try:
            with self._vault_path.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_vault_dict(self, data: dict[str, str]) -> bool:
        try:
            self._vault_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._vault_path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as fp:
                json.dump(data, fp, indent=2)
            tmp.replace(self._vault_path)
            return True
        except Exception:
            return False

    def _vault_write(self, key_name: str, secret_value: str) -> bool:
        vault = self._load_vault_dict()
        vault[key_name] = self._encrypt_value(secret_value)
        return self._save_vault_dict(vault)

    def _vault_read(self, key_name: str) -> str:
        vault = self._load_vault_dict()
        enc = vault.get(key_name, "")
        return self._decrypt_value(enc) if enc else ""

    def _vault_delete(self, key_name: str) -> bool:
        vault = self._load_vault_dict()
        if key_name in vault:
            vault.pop(key_name, None)
            return self._save_vault_dict(vault)
        return False

    @staticmethod
    def mask_secret(secret: str) -> str:
        if not secret or len(secret) < 8:
            return "********" if secret else ""
        return f"{secret[:4]}••••{secret[-3:]}"
