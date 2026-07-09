# Avisos de terceros

CapturaStudio (licencia MIT) se distribuye junto con software de terceros:

## FFmpeg
El instalador incluye `ffmpeg.exe` (compilación "full" de Gyan Doshi). FFmpeg es
software libre bajo **GPL v3** (esta compilación incluye componentes GPL).

CapturaStudio invoca FFmpeg como **programa independiente** mediante llamadas a
proceso (subprocess); no enlaza con sus librerías. Se trata de una *agregación*
de programas, por lo que CapturaStudio puede mantener su licencia MIT mientras
que `ffmpeg.exe` conserva su licencia GPL. El código fuente de FFmpeg está
disponible en https://ffmpeg.org y https://www.gyan.dev/ffmpeg/builds/.

- FFmpeg: https://ffmpeg.org — © los autores de FFmpeg, GPLv3.

## Modelos Whisper (whisper.cpp / GGML)
Los modelos de transcripción (ggml-tiny/base/small) se descargan bajo demanda
desde https://huggingface.co/ggerganov/whisper.cpp y se basan en **Whisper de
OpenAI** (licencia MIT) convertidos por el proyecto whisper.cpp (MIT).

## Librerías Python
Pillow (HPND), mss (MIT), numpy (BSD), soundcard (BSD), PyInstaller (GPL con
excepción de bootloader que permite distribuir ejecutables con cualquier licencia).
