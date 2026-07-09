"""Resumen y autoexamen de la leccion a partir del SRT (texto + tiempos).

Heuristica 100% local por defecto (TF-IDF/posicion + huecos cloze, sin LLM, en
numpy/stdlib). Si Ollama esta disponible, redacta un resumen y preguntas de mas
calidad. Honesto: sin LLM, las preguntas son mecanicas (un borrador util para
repasar), no equivalentes a un examen humano.
"""

from __future__ import annotations

import re
from html import escape

from . import llm

# Stopwords ES/EN minimas para el ranking de relevancia.
_STOP = set((
    "de la que el en y a los las un una por con para su al lo como mas pero sus le ya "
    "o este si porque esta entre cuando muy sin sobre tambien me hasta hay donde quien "
    "desde todo nos durante todos uno les ni contra otros ese eso ante ellos e esto mi "
    "the of to and a in is it you that he was for on are with as i his they be at one have "
    "this from or had by hot but some what there we can out other were all your when up use"
).split())

_WORD = re.compile(r"[a-záéíóúñ0-9]+", re.IGNORECASE)


def sentences(segments: list[tuple[float, float, str]]) -> list[tuple[float, str]]:
    """Frases con su tiempo de inicio aproximado (el del primer segmento que la abre)."""
    out: list[tuple[float, str]] = []
    buf, t0 = "", None
    for st, _en, txt in segments:
        if t0 is None:
            t0 = st
        buf = (buf + " " + txt).strip()
        # cerrar frase en signos de puntuacion fuertes
        while True:
            m = re.search(r"[.!?]+\s", buf)
            if not m:
                break
            frag = buf[:m.end()].strip()
            if len(frag) >= 15:
                out.append((t0, frag))
            buf = buf[m.end():].strip()
            t0 = st
    if len(buf) >= 15:
        out.append((t0 if t0 is not None else 0.0, buf))
    return out


def _keyword_freq(sents: list[tuple[float, str]]) -> dict:
    freq: dict[str, int] = {}
    for _t, s in sents:
        for w in _WORD.findall(s.lower()):
            if w not in _STOP and len(w) > 3:
                freq[w] = freq.get(w, 0) + 1
    return freq


def extractive_summary(segments, n: int = 5) -> list[str]:
    sents = sentences(segments)
    if not sents:
        return []
    freq = _keyword_freq(sents)

    def score(i):
        t, s = sents[i]
        ws = [w for w in _WORD.findall(s.lower()) if w in freq]
        base = sum(freq[w] for w in ws) / (len(ws) + 1)
        pos = 1.3 if i < max(1, len(sents) * 0.2) else 1.0   # arranque pesa mas
        return base * pos

    ranked = sorted(range(len(sents)), key=score, reverse=True)[:max(1, n)]
    return [sents[i][1] for i in sorted(ranked)]


def cloze_quiz(segments, n: int = 5) -> list[dict]:
    """Preguntas de hueco (oculta la palabra clave mas saliente) con su tiempo."""
    sents = sentences(segments)
    freq = _keyword_freq(sents)
    items: list[dict] = []
    for t, s in sents:
        cand = [w for w in _WORD.findall(s) if w.lower() in freq]
        if not cand:
            continue
        key = max(cand, key=lambda w: freq[w.lower()])
        if freq[key.lower()] < 2 or len(key) < 4:
            continue
        blanked = re.sub(r"\b" + re.escape(key) + r"\b", "______", s, count=1)
        items.append({"pregunta": blanked, "respuesta": key, "t": round(t, 1)})
        if len(items) >= n:
            break
    return items


# --- Orquestadores: Ollama si esta, si no la heuristica -------------------
def summarize(segments, n: int = 5) -> str:
    text = " ".join(t for _s, _e, t in segments).strip()
    if text and llm.available(timeout=1.5):
        out = llm.generate(
            "Resume esta transcripcion de una clase en 4-6 frases claras, en espanol, "
            "como apuntes para un alumno. Solo el resumen:\n\n" + text[:6000],
            system="Eres un profesor que redacta apuntes claros y concisos.")
        if out:
            return out
    pts = extractive_summary(segments, n)
    return "\n".join(f"- {p}" for p in pts) if pts else "(sin contenido suficiente)"


def quiz(segments, n: int = 5) -> list[dict]:
    text = " ".join(t for _s, _e, t in segments).strip()
    if text and llm.available(timeout=1.5):
        out = llm.generate(
            f"A partir de esta transcripcion, crea {n} preguntas de repaso con su respuesta, "
            "en espanol. Formato por linea 'P: ... | R: ...'. Solo las preguntas:\n\n" + text[:6000],
            system="Eres un profesor que crea preguntas de repaso utiles.")
        if out:
            parsed = []
            for ln in out.splitlines():
                m = re.search(r"P:\s*(.+?)\s*\|\s*R:\s*(.+)", ln)
                if m:
                    parsed.append({"pregunta": m.group(1).strip(),
                                   "respuesta": m.group(2).strip(), "t": None})
            if parsed:
                return parsed[:n]
    return cloze_quiz(segments, n)


def material_html(summary: str, quiz_items: list[dict], title: str = "") -> str:
    """Pagina autonoma con el resumen y el autoexamen (respuestas ocultas)."""
    sm = "".join(f"<p>{escape(line)}</p>" for line in summary.splitlines() if line.strip())
    qs = ""
    for i, it in enumerate(quiz_items, 1):
        ans = escape(str(it.get("respuesta", "")))
        qs += (f'<li>{escape(it.get("pregunta", ""))}'
               f'<button onclick="this.nextElementSibling.style.display=\'inline\'">ver</button>'
               f'<span class="a" style="display:none"> {ans}</span></li>')
    safe_title = escape(title or "Material de estudio")
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<title>{safe_title}</title><style>
 body{{font-family:Segoe UI,system-ui,sans-serif;max-width:820px;margin:0 auto;padding:24px;
   background:#0B1118;color:#E6EDF3}}
 h1{{color:#CE6E61}} h2{{color:#9bb;margin-top:28px}}
 li{{margin:8px 0}} .a{{color:#3FB950;font-weight:600}}
 button{{margin-left:8px;background:#1E3A5F;color:#fff;border:0;border-radius:6px;
   padding:2px 8px;cursor:pointer}}
</style></head><body>
 <h1>{safe_title}</h1>
 <h2>Resumen</h2>{sm}
 <h2>Autoexamen</h2><ol>{qs}</ol>
</body></html>"""
