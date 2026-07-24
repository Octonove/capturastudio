# CapturaStudio

Estudio de **grabación y streaming** de escritorio para **Windows**, 100% local, con
**superpoderes de IA** que OBS no tiene. Evolución de CapturaPro: en vez de pelear
por un compositor GPU en vivo, compone la escena en el **render** con FFmpeg
(calidad completa) y convierte una toma en un paquete de contenido.

> 100% en tu PC · sin marca de agua · sin límites · código abierto (MIT)

## ⬇️ Descargar (Windows 10/11)

### ➡️ [**Descargar CapturaStudio (instalador .exe)**](https://github.com/Octonove/capturastudio/releases/latest/download/CapturaStudio-Setup.exe)

Descarga **directa** del instalador, sin registro. También puedes ver la [última versión y notas](https://github.com/Octonove/capturastudio/releases/latest).

> Si Windows muestra *"Windows protegió tu PC"* (es normal en programas nuevos sin firma): pulsa **Más información → Ejecutar de todas formas**. Se instala sin permisos de administrador.

---

## Funciones

### Tres modos (elige tu camino)
Al abrir la app eliges un modo (y puedes cambiar desde el menú **🚀 Modos**):
- **🎓 Docente** — graba tu clase y, al parar, la IA local la pule (sin editar).
- **📚 Curso para YouTube** — como Docente, y además genera **capítulos por tema +
  índice clicable** listos para YouTube.
- **🎬 Streamer / Estudio** — el estudio completo: escenas por capas, chroma, directo
  multidestino y buffer de repetición.

### Escena y grabación
- **Fuentes** componibles por capas: pantalla/monitor, **captura de una ventana**
  (elige la app de una lista de las que tienes abiertas), **webcam** (recorte
  circular o **chroma key**), imagen (con alfa), texto, color y vídeo/media.
- **Captura de ventana tipo OBS** (Windows Graphics Capture): graba solo esa
  ventana **aunque la tapes con otra** y **la sigue** si la mueves o **cambia de
  título** (navegadores que cambian de pestaña), también con apps aceleradas por
  GPU. Funciona igual en **grabación, streaming en directo y replay**, y el vídeo
  **mantiene la velocidad real** aunque el PC no llegue a los FPS pedidos. En
  equipos antiguos cae automáticamente a la captura por región.
- **Texto personalizable**: color del texto, **fondo** activable con su propio
  color y **opacidad de fondo independiente** (texto opaco sobre fondo tenue), y
  edición del texto/tamaño después de crearlo.
- **Recorte de la fuente**: marca en un clic la zona a grabar (p. ej. solo el
  contenido de una ventana, sin barras ni pestañas).
- Editor de **layout** con inspector (posición, **tamaño ancho y alto**, forma,
  opacidad) y **vista previa de encuadre** en vivo fiel a lo que se graba
  (arrastrar/redimensionar las fuentes con el ratón).
- **Múltiples escenas** (slots) en un mismo proyecto: crea, duplica, renombra y
  **conmuta** entre ellas; el proyecto entero se guarda/carga en un `.json`.
- Grabación a **calidad nativa** (NVENC / AMF / QSV / x264, CRF/CQ), **pausa y
  reanudación** sin pérdida, y **audio** de sistema (loopback WASAPI) + micrófono.
- **Medidores VU** en vivo (sistema y micro) y **grabación programada**
  (empezar a una hora / dentro de N min, con parada automática opcional).

### Directo (streaming)
- Salida a **Twitch, YouTube, Facebook, Kick** o RTMP/RTMPS personalizado —
  solo pegas tu *stream key*.
- Audio en vivo, **grabación de VOD** simultánea (.mkv) y **reconexión** automática.

### Post-producción con IA local (el foso)
- **Subtítulos automáticos** (Whisper) → `.srt`, opción de **quemarlos** y traducir a inglés.
- **Recorte de silencios** (auto-jumpcut): un tutorial con pausas queda fluido en un clic.
- **Auto-encuadre** que **sigue al sujeto** (recorte dinámico, también en vertical 9:16),
  por detección de movimiento — sin GPU ni dependencias extra.
- **Capítulos automáticos** por tema → `capítulos.txt` (YouTube), índice HTML clicable
  y capítulos incrustados en el MP4.
- **Control de calidad**: la app se audita sola (audio mudo/saturado, sin voz, pantalla
  en negro) y ofrece **arreglo de un clic** (normalizar audio).
- **Escudo de privacidad** (difumina datos sensibles, incl. retroactivo) y **foco de
  ventana** (oscurece todo menos lo que explicas).
- **Fábrica de contenido**: una grabación → vertical 9:16 + audio MP3 + SRT.
- **Exportar a GIF**: convierte una grabación (el **vídeo completo** o un **tramo** a
  elegir) en un **GIF optimizado** (paleta de 2 pasadas, buena calidad), con **ancho y
  fps** ajustables — ideal para demos y previews. La grabación se guarda en MP4; el GIF
  es una conversión en post.
- Todo **offline y privado**: ni una palabra sale de tu PC.

### Atajos globales (remapeables)
Funcionan aunque la app no tenga el foco. Se pueden **reasignar** desde
*Ayuda → Atajos de teclado* (captura la combinación al vuelo).

| Atajo por defecto | Acción |
|---|---|
| `Ctrl+Shift+R` | Iniciar / detener grabación |
| `Ctrl+Shift+P` | Pausar / reanudar |
| `Ctrl+Shift+D` | Iniciar / detener directo |
| `Ctrl+Shift+M` | Guardar momento (replay) |

## Ejecutar en desarrollo
```powershell
./run.ps1
```
(usa el venv de CapturaPro, que comparte dependencias; o crea uno con `requirements.txt`)

## Construir el ejecutable (.exe)
```powershell
./build/build.ps1
```
→ `dist\CapturaStudio\CapturaStudio.exe` (FFmpeg incluido, portable).

## Crear el instalador único
```powershell
./build/build-installer.ps1
```
→ `installer\CapturaStudio-Setup-1.0.0.exe` (instala sin admin, con accesos directos y desinstalador).
Requiere [Inno Setup](https://jrsoftware.org/isinfo.php).

## Notas técnicas
- **Compositing en el render**: la escena (modelo declarativo de capas) se traduce a
  un `filter_complex` de FFmpeg (overlay/scale/crop/máscara circular/opacidad) que
  se materializa al grabar o emitir. El vídeo final va a fps completos.
- **Audio en directo**: micro+sistema se mezclan en Python y se canalizan por
  `stdin` (s16le) al proceso de FFmpeg.
- **IA local**: filtro `whisper` de FFmpeg para subtítulos y `silencedetect` para
  el recorte. El modelo se descarga la primera vez (robusto frente a antivirus que
  interceptan TLS: `curl --ssl-no-revoke` → .NET → urllib).

## Stack
Python 3.14 + Tkinter + FFmpeg (full build) + Pillow + mss + numpy + soundcard +
PyInstaller + Inno Setup. Licencia **MIT** (ver `LICENSE` y `THIRD-PARTY-NOTICES.md`).
