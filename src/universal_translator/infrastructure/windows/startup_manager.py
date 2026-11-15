"""Gerenciamento de inicialização configurável com o Windows (Registro HKCU Run)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


class WindowsStartupManager:
    """Configura ou remove a inicialização automática do Universal Translator Pro no logon do usuário Windows.

    Não depende de arquivos temporários nem de caminhos absolutos de desenvolvimento quando empacotado.
    """

    RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
    APP_ENTRY_NAME = "UniversalTranslatorPro"

    def __init__(self, winreg_module: Any = None, is_win32: bool | None = None) -> None:
        self._is_win32 = (sys.platform == "win32") if is_win32 is None else is_win32
        self._winreg = winreg_module if winreg_module is not None else self._load_winreg()
        self._simulated_state: str | None = None

    @staticmethod
    def _load_winreg() -> Any:
        try:
            import winreg  # type: ignore
            return winreg
        except Exception:
            return None

    @staticmethod
    def build_startup_command(executable_path: Path | None = None) -> str:
        """Gera o comando de inicialização sem depender de arquivos temporários."""
        if executable_path is not None:
            return f'"{executable_path.resolve()}"'
        if getattr(sys, "frozen", False):
            return f'"{Path(sys.executable).resolve()}"'
        main_script = Path(__file__).resolve().parents[3] / "main.py"
        py_exe = Path(sys.executable).resolve()
        if py_exe.name.lower() == "python.exe":
            pyw = py_exe.with_name("pythonw.exe")
            if pyw.exists():
                py_exe = pyw
        return f'"{py_exe}" "{main_script}"'

    def is_enabled(self) -> bool:
        if not self._is_win32 or self._winreg is None:
            return bool(self._simulated_state)
        try:
            with self._winreg.OpenKey(
                self._winreg.HKEY_CURRENT_USER,
                self.RUN_KEY_PATH,
                0,
                self._winreg.KEY_READ,
            ) as key:
                val, _ = self._winreg.QueryValueEx(key, self.APP_ENTRY_NAME)
                return bool(val and str(val).strip())
        except OSError:
            return False

    def set_enabled(self, enabled: bool, executable_path: Path | None = None) -> bool:
        cmd = self.build_startup_command(executable_path)
        if not self._is_win32 or self._winreg is None:
            self._simulated_state = cmd if enabled else None
            return True
        try:
            with self._winreg.OpenKey(
                self._winreg.HKEY_CURRENT_USER,
                self.RUN_KEY_PATH,
                0,
                self._winreg.KEY_SET_VALUE,
            ) as key:
                if enabled:
                    self._winreg.SetValueEx(
                        key,
                        self.APP_ENTRY_NAME,
                        0,
                        self._winreg.REG_SZ,
                        cmd,
                    )
                else:
                    try:
                        self._winreg.DeleteValue(key, self.APP_ENTRY_NAME)
                    except OSError:
                        pass
            return True
        except OSError:
            return False
