"""Capitulos automaticos: trata el SRT de Whisper como datos (texto + tiempos) y
deriva capitulos por tema. Diferencial frente a OBS, que entrega un archivo opaco.

Heuristica 100% local (sin LLM): los huecos de silencio largos entre frases son
fronteras naturales de tema; el titulo de cada bloque es su primera frase (es un
borrador editable, honesto: no esta "redactado"). Genera 3 entregables:
  - capitulos.txt  (formato YouTube: "MM:SS Titulo", el primero a 00:00)
  - indice.html    (lista clicable que salta al segundo del MP4 local)
  - <video>_cap.mp4 (capitulos incrustados via metadata de FFmpeg)
"""

from __future__ import annotations

import logging
import re
import subprocess
from html import escape
from pathlib import Path
from urllib.parse import quote

from . import ffmpeg_utils as fu

logger = logging.getLogger(__name__)


class ChaptersError(Exception):
    pass


_TS = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})")


def _to_sec(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / (10 ** len(ms))


def parse_srt(text: str) -> list[tuple[float, float, str]]:
    """Devuelve [(start, end, texto)] del SRT. Tolerante a numeracion/espacios."""
    segments: list[tuple[float, float, str]] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip())
    for blk in blocks:
        m = _TS.search(blk)
        if not m:
            continue
        g = m.groups()
        start = _to_sec(g[0], g[1], g[2], g[3])
        end = _to_sec(g[4], g[5], g[6], g[7])
        # el texto es lo que va despues de la linea de tiempo
        after = blk[m.end():].strip()
        txt = " ".join(line.strip() for line in after.split("\n") if line.strip())
        if txt:
            segments.append((start, end, txt))
    return segments


def _title_from(text: str, max_len: int = 60) -> str:
    t = re.sub(r"\s+", " ", text).strip().rstrip(".,;:")
    if len(t) > max_len:
        t = t[:max_len - 1].rstrip() + "…"
    return t or "Capitulo"


def _time_chapters(segments: list[tuple[float, float, str]], n: int) -> list[tuple[float, str]]:
    """Reparte n capitulos por TIEMPO (para narracion continua sin silencios, tipica
    de YouTube). El titulo de cada uno es la frase que se esta diciendo en esa marca."""
    duration = segments[-1][1]
    chapters: list[tuple[float, str]] = []
    for k in range(n):
        b = duration * k / n
        seg = segments[0]
        for s in segments:            # ultima frase que empieza en o antes de la marca
            if s[0] <= b:
                seg = s
            else:
                break
        chapters.append((round(b, 2), _title_from(seg[2])))
    chapters[0] = (0.0, chapters[0][1])
    return chapters


def group_chapters(segments: list[tuple[float, float, str]], *, min_gap: float = 2.5,
                   min_len: float = 12.0, max_n: int = 30,
                   target_len: float = 75.0) -> list[tuple[float, str]]:
    """Agrupa segmentos en capitulos. Frontera = hueco de silencio > min_gap.
    Garantiza que el primero empieza en 0 y fusiona capitulos demasiado cortos.
    Si el video dura bastante pero apenas hay silencios (narracion continua), cae a
    un reparto por tiempo cada ~target_len s para que el indice sea util igualmente."""
    if not segments:
        return [(0.0, "Capitulo 1")]
    chapters: list[tuple[float, str]] = [(0.0, _title_from(segments[0][2]))]
    prev_end = segments[0][1]
    for start, end, txt in segments[1:]:
        if start - prev_end >= min_gap and start - chapters[-1][0] >= min_len:
            chapters.append((start, _title_from(txt)))
        prev_end = end
    # Respaldo por tiempo: los videos editados (YouTube) casi no tienen silencios
    # largos, asi que la deteccion por huecos daria un solo capitulo (0:00). Solo si
    # la deteccion por tema FALLO (quedo el unico 0:00) y el video da para varios,
    # repartimos por tiempo. Si encontro >=2 fronteras reales, se respetan.
    duration = segments[-1][1]
    desired = int(duration // target_len)
    if len(chapters) < 2 and desired >= 2:
        chapters = _time_chapters(segments, min(desired, max_n))
    # recortar si hay demasiados (mantener los mas separados no es trivial; cap simple)
    if len(chapters) > max_n:
        step = len(chapters) / max_n
        chapters = [chapters[int(i * step)] for i in range(max_n)]
        chapters[0] = (0.0, chapters[0][1])
    return chapters


def _fmt_hms(sec: float) -> str:
    sec = int(sec)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def youtube_txt(chapters: list[tuple[float, str]]) -> str:
    # YouTube exige que el primer capitulo sea 0:00
    lines = []
    for i, (t, title) in enumerate(chapters):
        stamp = "0:00" if i == 0 else _fmt_hms(t)
        lines.append(f"{stamp} {title}")
    return "\n".join(lines) + "\n"


def index_html(chapters: list[tuple[float, str]], video_filename: str, title: str = "") -> str:
    rows = "\n".join(
        f'    <li><a href="#" onclick="j({t:.2f});return false">'
        f'<span class="t">{escape(_fmt_hms(t))}</span> {escape(ti)}</a></li>'
        for t, ti in chapters)
    # URL-encode (espacios, '#', '%'…) y luego escapar para el atributo HTML
    safe_vid = escape(quote(video_filename), quote=True)
    safe_title = escape(title or video_filename)
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<title>{safe_title} — capitulos</title>
<style>
 body{{font-family:Segoe UI,system-ui,sans-serif;margin:0;background:#0B1118;color:#E6EDF3}}
 .wrap{{max-width:980px;margin:0 auto;padding:18px}}
 video{{width:100%;border-radius:10px;background:#000}}
 h1{{font-size:18px;color:#CE6E61}}
 ul{{list-style:none;padding:0}}
 li a{{display:flex;gap:12px;padding:9px 12px;color:#E6EDF3;text-decoration:none;border-radius:8px}}
 li a:hover{{background:#1E3A5F}}
 .t{{color:#8aa;min-width:56px;font-variant-numeric:tabular-nums}}
</style></head><body><div class="wrap">
 <h1>{safe_title}</h1>
 <video id="v" src="{safe_vid}" controls></video>
 <ul>
{rows}
 </ul>
 <script>function j(t){{var v=document.getElementById('v');v.currentTime=t;v.play();window.scrollTo(0,0);}}</script>
</div></body></html>"""


def _ffmeta_escape(s: str) -> str:
    """FFMETADATA exige escapar '=', ';', '#', '\\' y los saltos de linea."""
    s = s.replace("\\", "\\\\")
    for ch_ in ("=", ";", "#"):
        s = s.replace(ch_, "\\" + ch_)
    return s.replace("\n", " ").replace("\r", " ")


def search_html(segments: list[tuple[float, float, str]], video_filename: str,
                title: str = "") -> str:
    """Buscador OFFLINE: HTML autonomo que filtra el transcript por palabra y
    salta al segundo exacto del video local. Sin servidor, todo embebido."""
    import json as _json
    data = _json.dumps([{"t": round(s, 2), "x": txt} for s, _e, txt in segments],
                       ensure_ascii=False)
    data = data.replace("</", "<\\/")   # evita que '</script>' del SRT parta la pagina
    # (lo anterior basta; el '<\/script>' resultante es identico para JSON.parse/JS)
    safe_vid = escape(quote(video_filename), quote=True)
    safe_title = escape(title or video_filename)
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<title>{safe_title} — buscar</title>
<style>
 body{{font-family:Segoe UI,system-ui,sans-serif;margin:0;background:#0B1118;color:#E6EDF3}}
 .wrap{{max-width:980px;margin:0 auto;padding:18px}}
 video{{width:100%;border-radius:10px;background:#000}}
 h1{{font-size:18px;color:#CE6E61}}
 input{{width:100%;padding:11px 14px;border-radius:8px;border:1px solid #1E3A5F;
   background:#0B1118;color:#E6EDF3;font-size:15px;margin:10px 0}}
 .r{{display:flex;gap:12px;padding:9px 12px;border-radius:8px;cursor:pointer}}
 .r:hover{{background:#1E3A5F}}
 .t{{color:#8aa;min-width:56px;font-variant-numeric:tabular-nums}}
 mark{{background:#CE6E61;color:#fff;border-radius:3px}}
</style></head><body><div class="wrap">
 <h1>{safe_title}</h1>
 <video id="v" src="{safe_vid}" controls></video>
 <input id="q" placeholder="Busca una palabra dicha en el video…" autofocus>
 <div id="out"></div>
 <script>
 var D={data}, v=document.getElementById('v'), out=document.getElementById('out');
 function fmt(t){{t=Math.floor(t);var m=Math.floor(t/60),s=t%60;return m+':'+(s<10?'0':'')+s;}}
 function go(t){{v.currentTime=t;v.play();window.scrollTo(0,0);}}
 function render(q){{
   out.innerHTML=''; q=q.trim().toLowerCase(); if(!q)return;
   var n=0;
   for(var i=0;i<D.length && n<200;i++){{
     if(D[i].x.toLowerCase().indexOf(q)<0)continue; n++;
     var d=document.createElement('div'); d.className='r';
     var txt=D[i].x.replace(new RegExp('('+q.replace(/[.*+?^${{}}()|[\\]\\\\]/g,'\\\\$&')+')','ig'),'<mark>$1</mark>');
     d.innerHTML='<span class="t">'+fmt(D[i].t)+'</span><span>'+txt+'</span>';
     (function(t){{d.onclick=function(){{go(t);}};}})(D[i].t);
     out.appendChild(d);
   }}
   if(!n)out.innerHTML='<div class="r">Sin resultados.</div>';
 }}
 document.getElementById('q').addEventListener('input',function(e){{render(e.target.value);}});
 </script>
</div></body></html>"""


def ffmetadata(chapters: list[tuple[float, str]], total: float) -> str:
    out = [";FFMETADATA1"]
    for i, (t, title) in enumerate(chapters):
        end = chapters[i + 1][0] if i + 1 < len(chapters) else total
        out += ["[CHAPTER]", "TIMEBASE=1/1000",
                f"START={int(t * 1000)}", f"END={int(max(t + 1, end) * 1000)}",
                f"title={_ffmeta_escape(title)}"]
    return "\n".join(out) + "\n"


def embed_chapters(ffmpeg: str, video: str, meta_text: str, out_path: str) -> None:
    """Incrusta los capitulos como metadata (sin recodificar)."""
    meta_path = Path(out_path).with_suffix(".ffmeta.txt")
    meta_path.write_text(meta_text, encoding="utf-8")
    # copia el video; re-codifica el audio a AAC para que el muxer mp4 lo acepte
    # siempre (la fuente puede traer audio no-MP4 si es un video importado).
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", video,
           "-i", str(meta_path), "-map_metadata", "1", "-map_chapters", "1",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", out_path]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=300, **fu.subprocess_kwargs())
    except (OSError, subprocess.SubprocessError) as exc:
        raise ChaptersError(f"No se pudieron incrustar capitulos: {exc}") from exc
    finally:
        try:
            meta_path.unlink(missing_ok=True)
        except OSError:
            pass
    if proc.returncode != 0 or not Path(out_path).is_file():
        raise ChaptersError(fu._decode(proc.stderr)[-300:] or "No se pudieron incrustar capitulos.")


def make_chapters(ffmpeg: str, video: str, srt_text: str, out_dir: str, *,
                  total: float = 0.0, embed: bool = True) -> dict:
    """Genera capitulos.txt, indice.html y (opcional) el MP4 con capitulos."""
    segs = parse_srt(srt_text)
    chapters = group_chapters(segs)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = Path(video).stem
    created = {}

    txt_p = out / f"{stem}_capitulos.txt"
    txt_p.write_text(youtube_txt(chapters), encoding="utf-8")
    created["youtube_txt"] = str(txt_p)

    # Incrustar PRIMERO: asi el indice puede referenciar el MP4 con capitulos que
    # vive en la MISMA carpeta que el html (evita rutas relativas rotas).
    video_ref = None
    if embed:
        if total <= 0:
            from . import ai_post
            total = ai_post.get_duration(ffmpeg, video) or (chapters[-1][0] + 60)
        mp4_p = out / f"{stem}_cap.mp4"
        embed_chapters(ffmpeg, video, ffmetadata(chapters, total), str(mp4_p))
        created["mp4"] = str(mp4_p)
        video_ref = mp4_p.name                 # esta junto al html
    else:
        # sin MP4 propio: el original esta en la carpeta padre del html
        video_ref = f"../{Path(video).name}"

    html_p = out / f"{stem}_indice.html"
    html_p.write_text(index_html(chapters, video_ref, stem), encoding="utf-8")
    created["index_html"] = str(html_p)

    return {"chapters": chapters, "files": created}
