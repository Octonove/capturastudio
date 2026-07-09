"""Valida la logica del preview interactivo: hit-test, arrastrar y redimensionar
(simulando eventos de raton, sin GUI visible)."""

import sys
from capturastudio.monitors import set_dpi_awareness

set_dpi_awareness()
from capturastudio.app import App  # noqa: E402
from capturastudio import scene as scn  # noqa: E402


class E:
    def __init__(self, x, y):
        self.x, self.y = x, y


def main() -> int:
    app = App()
    app.withdraw()
    app.update_idletasks()
    app.update()
    app._render_preview()
    if not app._boxes:
        print("Sin cajas de preview."); return 1

    sid = next((s.id for s in app.scene.visible_sorted() if s.kind != scn.KIND_SCREEN), None)
    if not sid:
        print("Sin fuente movible (webcam)."); return 1
    s = next(x for x in app.scene.sources if x.id == sid)

    # --- mover ---
    bx, by, bw, bh = app._boxes[sid]
    px, py = app._to_preview(bx + bw / 2, by + bh / 2)
    x0 = s.transform.x
    app._on_canvas_press(E(px, py))
    sel_ok = app._sel_id == sid and app._drag and app._drag[0] == "move"
    app._on_canvas_drag(E(px + 40, py + 10))
    app._on_canvas_release(E(px + 40, py + 10))
    moved = s.transform.x != x0
    print(f"seleccion+mover: sel={app._sel_id == sid} movido={moved} ({x0} -> {s.transform.x})")

    # --- redimensionar (esquina) ---
    app._select_id(sid)
    app._render_preview()
    bx, by, bw, bh = app._boxes[sid]
    hx, hy = app._to_preview(bx + bw, by + bh)
    w0 = s.transform.w
    app._on_canvas_press(E(hx, hy))
    res_drag = bool(app._drag and app._drag[0] == "resize")
    app._on_canvas_drag(E(hx + 30, hy + 30))
    app._on_canvas_release(E(hx + 30, hy + 30))
    resized = s.transform.w != w0
    print(f"redimensionar: handle={res_drag} cambio={resized} ({w0} -> {s.transform.w})")

    app.destroy()
    ok = sel_ok and moved and res_drag and resized
    print("\nVEREDICTO:", "PREVIEW INTERACTIVO OK" if ok else "REVISAR")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
