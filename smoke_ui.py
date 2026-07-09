"""Smoke test de la UI: construye la ventana (oculta), ejecuta un ciclo de
preview y la cierra. Valida imports y construccion sin abrir nada visible."""

import sys
from capturastudio.monitors import set_dpi_awareness

set_dpi_awareness()
from capturastudio.app import App  # noqa: E402

try:
    app = App()
    app.withdraw()  # no mostrar la ventana
    app.update_idletasks()
    app.update()
    app._render_preview()  # un ciclo de preview real (mss + Pillow)
    app.update()
    srcs = [s.label() for s in app.scene.sources]
    print("UI construida OK. Encoder choices:", sorted(app.encoders))
    print("Webcams:", app.video_devices)
    print("Fuentes de la escena:", srcs)
    print("Preview renderizado:", app._preview_imgtk is not None)
    app.destroy()
    print("SMOKE UI: OK")
except Exception as exc:  # noqa: BLE001
    import traceback
    traceback.print_exc()
    print("SMOKE UI: FALLO:", exc)
    sys.exit(1)
