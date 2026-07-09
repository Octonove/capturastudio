"""Atajos de teclado globales para Windows (RegisterHotKey via ctypes).

Funcionan aunque la app no tenga el foco. Un hilo dedicado registra los atajos y
corre su propio bucle de mensajes; las pulsaciones se encolan y se despachan en
el hilo de Tk mediante un sondeo con root.after.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
import tkinter as tk
from ctypes import wintypes

logger = logging.getLogger(__name__)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

# --- Mapa de teclas <-> virtual-key codes (para remapear atajos) ----------
NAME_VKS: dict[str, int] = {}
VK_NAMES: dict[int, str] = {}
for _c in range(ord("A"), ord("Z") + 1):      # letras
    NAME_VKS[chr(_c)] = _c; VK_NAMES[_c] = chr(_c)
for _c in range(ord("0"), ord("9") + 1):      # digitos fila superior
    NAME_VKS[chr(_c)] = _c; VK_NAMES[_c] = chr(_c)
for _i in range(1, 25):                       # F1..F24
    NAME_VKS[f"F{_i}"] = 0x6F + _i; VK_NAMES[0x6F + _i] = f"F{_i}"

# Orden canonico de modificadores al formatear.
_MODS_ORDER = (("Ctrl", MOD_CONTROL), ("Alt", MOD_ALT), ("Shift", MOD_SHIFT), ("Win", MOD_WIN))
_MOD_ALIASES = {"CTRL": MOD_CONTROL, "CONTROL": MOD_CONTROL, "SHIFT": MOD_SHIFT,
                "ALT": MOD_ALT, "WIN": MOD_WIN, "SUPER": MOD_WIN, "META": MOD_WIN, "CMD": MOD_WIN}


def parse_hotkey(text: str):
    """'Ctrl+Shift+R' -> (modifiers_int, vk). Devuelve None si es invalido.

    Exige al menos un modificador (un atajo GLOBAL sin modificador secuestraria
    la tecla en todo el sistema) y exactamente una tecla reconocida."""
    if not text:
        return None
    parts = [p.strip() for p in str(text).replace("-", "+").split("+") if p.strip()]
    mods, vk = 0, None
    for p in parts:
        up = p.upper()
        if up in _MOD_ALIASES:
            mods |= _MOD_ALIASES[up]
        elif up in NAME_VKS:
            if vk is not None:
                return None          # mas de una tecla no-modificadora
            vk = NAME_VKS[up]
        else:
            return None
    if vk is None or mods == 0:
        return None
    return (mods, vk)


def format_hotkey(mods: int, vk: int) -> str:
    out = [name for name, bit in _MODS_ORDER if mods & bit]
    out.append(VK_NAMES.get(vk, "?"))
    return "+".join(out)


def keysym_to_vk(keysym: str):
    """Convierte un keysym de Tk ('r', 'F5', '3') al vk de RegisterHotKey."""
    if not keysym:
        return None
    return NAME_VKS.get(keysym.upper())


def validate_hotkey_map(mapping: dict) -> tuple[bool, str]:
    """Cada atajo debe ser valido y ninguna combinacion (mods, vk) puede repetirse.

    Compara por el (mods, vk) normalizado, no por el texto: 'Ctrl+Shift+R' y
    'Shift+Ctrl+R' son la MISMA combinacion y chocarian en RegisterHotKey."""
    seen: dict[tuple, str] = {}
    for action, combo in mapping.items():
        parsed = parse_hotkey(combo)
        if not parsed:
            return (False, f"Atajo invalido: {combo or '(vacio)'}")
        if parsed in seen:
            return (False, f"'{combo}' esta repetido (misma combinacion en dos acciones).")
        seen[parsed] = action
    return (True, "")


class GlobalHotkeys:
    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self._bindings: list[tuple[int, int, int, object, str]] = []
        self._queue: queue.Queue[int] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._started = threading.Event()
        self._next_id = 1
        self._running = False

    def add(self, modifiers: int, vk: int, callback, name: str = "") -> None:
        hid = self._next_id
        self._next_id += 1
        self._bindings.append((hid, modifiers | MOD_NOREPEAT, vk, callback, name))

    def start(self) -> None:
        if self._running or not self._bindings:
            return
        self._running = True
        self._started.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._poll()

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        self._started.set()
        registered: list[int] = []
        for hid, mod, vk, _cb, name in self._bindings:
            if user32.RegisterHotKey(None, hid, mod, vk):
                registered.append(hid)
            else:
                logger.warning("No se pudo registrar el atajo %s (en uso por otra app).", name)
        msg = wintypes.MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
            if msg.message == WM_HOTKEY:
                self._queue.put(int(msg.wParam))
        for hid in registered:
            user32.UnregisterHotKey(None, hid)

    def _poll(self) -> None:
        try:
            while True:
                self._dispatch(self._queue.get_nowait())
        except queue.Empty:
            pass
        if self._running:
            try:
                self.root.after(120, self._poll)
            except tk.TclError:
                pass

    def _dispatch(self, hid: int) -> None:
        for b in self._bindings:
            if b[0] == hid:
                try:
                    b[3]()
                except Exception:  # noqa: BLE001
                    logger.exception("Error ejecutando el atajo %s", b[4])
                return

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._started.wait(timeout=2.0)
        if self._thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except OSError:
                pass
        # Esperar a que el hilo procese WM_QUIT y ejecute UnregisterHotKey ANTES
        # de volver: si no, un re-registro inmediato (remapeo) colisiona con los
        # atajos aun vivos y RegisterHotKey falla en silencio.
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        # Resetear estado: la instancia queda reutilizable con start().
        self._thread = None
        self._thread_id = None
        self._started.clear()
