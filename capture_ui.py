"""Abre la ventana brevemente y captura una imagen real de la UI."""

import time
from pathlib import Path
from capturastudio.monitors import set_dpi_awareness

set_dpi_awareness()
from capturastudio.app import App  # noqa: E402
import mss  # noqa: E402
from PIL import Image  # noqa: E402

app = App()
app.update_idletasks()
app.geometry("1180x870+40+20")
app.deiconify()
app.lift()
app.attributes("-topmost", True)
for _ in range(12):
    app.update()
    time.sleep(0.12)
x, y = app.winfo_rootx(), app.winfo_rooty()
w, h = app.winfo_width(), app.winfo_height()
out = Path(__file__).resolve().parent / "prototype" / "out" / "ui_shot.png"
with mss.mss() as sct:
    grab = sct.grab({"left": x - 2, "top": y - 32, "width": w + 4, "height": h + 36})
    Image.frombytes("RGB", grab.size, grab.bgra, "raw", "BGRX").save(out)
print("UI shot:", out, out.is_file())
app.destroy()
