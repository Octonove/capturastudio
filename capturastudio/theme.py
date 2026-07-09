"""Sistema de diseno de CapturaStudio: tema navy/terracota compartido de la suite
(octonove_core.theme) + la variante COMPACTA propia de Rec.TButton."""

from __future__ import annotations

from tkinter import ttk

from octonove_core.theme import *  # noqa: F401,F403
from octonove_core.theme import apply as _core_apply
from octonove_core.theme import F_BTN, REC, WHITE


def apply(root) -> None:
    _core_apply(root)
    st = ttk.Style(root)
    # Variante compacta propia: el boton '● Grabar' vive en un cluster de controles
    # dimensionado para 10pt/padding(16,9); la variante grande del core lo descuadra.
    st.configure("Rec.TButton", font=F_BTN, padding=(16, 9), relief="flat",
                 background=REC, foreground=WHITE, bordercolor=REC)
