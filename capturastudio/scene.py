"""Modelo de datos de la ESCENA: fuentes (sources) y sus transformaciones.

Una escena es una lista de fuentes apiladas por z-order sobre un lienzo. El
motor (ffmpeg_utils.build_scene) traduce esto a un filter_complex de FFmpeg que
compone todo en el render. NO hay compositor GPU en vivo: el modelo es
declarativo y FFmpeg lo materializa al grabar/emitir.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field, asdict

_ids = itertools.count(1)

# Tipos de fuente soportados.
KIND_SCREEN = "screen"   # captura de monitor/region (gdigrab)
KIND_WINDOW = "window"   # captura de ventana por titulo (gdigrab)
KIND_WEBCAM = "webcam"   # dispositivo de video (dshow)
KIND_IMAGE = "image"     # imagen (png/jpg) con alfa
KIND_TEXT = "text"       # texto renderizado (Pillow -> png)
KIND_COLOR = "color"     # color/relleno solido
KIND_MEDIA = "media"     # video/audio de fichero

KIND_LABELS = {
    KIND_SCREEN: "Pantalla", KIND_WINDOW: "Ventana", KIND_WEBCAM: "Webcam",
    KIND_IMAGE: "Imagen", KIND_TEXT: "Texto", KIND_COLOR: "Color", KIND_MEDIA: "Video/Media",
}


def _parse_crop(c):
    """Devuelve (x,y,w,h) de enteros o None. Un crop malformado se descarta en
    vez de propagarse y romper luego el filter_complex de FFmpeg."""
    if not c:
        return None
    try:
        vals = tuple(int(x) for x in c)
    except (TypeError, ValueError):
        return None
    return vals if len(vals) == 4 else None


@dataclass
class Transform:
    x: int = 0            # posicion en el lienzo (px)
    y: int = 0
    w: int = 0            # tamano en el lienzo (0 = auto desde la fuente)
    h: int = 0
    opacity: float = 1.0  # 0..1
    shape: str = "rect"   # rect | circle  (mascara de recorte)
    crop: tuple[int, int, int, int] | None = None  # (x,y,w,h) recorte de la fuente
    chroma: str | None = None  # color clave para chroma key (#hex) o None


@dataclass
class Source:
    kind: str
    name: str = ""
    params: dict = field(default_factory=dict)   # parametros segun kind
    transform: Transform = field(default_factory=Transform)
    visible: bool = True
    z: int = 0
    id: int = field(default_factory=lambda: next(_ids))

    def label(self) -> str:
        return self.name or KIND_LABELS.get(self.kind, self.kind)


@dataclass
class Scene:
    name: str = "Escena 1"
    canvas_w: int = 1920
    canvas_h: int = 1080
    fps: int = 30
    bg_color: str = "0x101418"
    sources: list[Source] = field(default_factory=list)

    def visible_sorted(self) -> list[Source]:
        return [s for s in sorted(self.sources, key=lambda s: s.z) if s.visible]

    def add(self, source: Source) -> Source:
        source.z = (max((s.z for s in self.sources), default=0) + 1) if self.sources else 0
        self.sources.append(source)
        return source

    def remove(self, source_id: int) -> None:
        self.sources = [s for s in self.sources if s.id != source_id]

    def raise_(self, source_id: int) -> None:
        self._reorder(source_id, +1)

    def lower(self, source_id: int) -> None:
        self._reorder(source_id, -1)

    def _reorder(self, source_id: int, direction: int) -> None:
        ordered = sorted(self.sources, key=lambda s: s.z)
        idx = next((i for i, s in enumerate(ordered) if s.id == source_id), None)
        if idx is None:
            return
        j = idx + direction
        if 0 <= j < len(ordered):
            ordered[idx], ordered[j] = ordered[j], ordered[idx]
            for k, s in enumerate(ordered):
                s.z = k

    def to_dict(self) -> dict:
        return {
            "name": self.name, "canvas_w": self.canvas_w, "canvas_h": self.canvas_h,
            "fps": self.fps, "bg_color": self.bg_color,
            "sources": [
                {**{k: v for k, v in asdict(s).items() if k != "transform"},
                 "transform": asdict(s.transform)}
                for s in self.sources
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Scene":
        sc = cls(name=d.get("name", "Escena 1"),
                 canvas_w=int(d.get("canvas_w", 1920)),
                 canvas_h=int(d.get("canvas_h", 1080)),
                 fps=int(d.get("fps", 30)),
                 bg_color=d.get("bg_color", "0x101418"))
        for sd in d.get("sources", []):
            kind = sd.get("kind")
            if not kind:
                continue   # fuente corrupta: omitir sin abortar el proyecto entero
            td = sd.get("transform", {})
            t = Transform(x=int(td.get("x", 0)), y=int(td.get("y", 0)),
                          w=int(td.get("w", 0)), h=int(td.get("h", 0)),
                          opacity=float(td.get("opacity", 1.0)),
                          shape=td.get("shape", "rect"),
                          crop=_parse_crop(td.get("crop")),
                          chroma=td.get("chroma"))
            sc.sources.append(Source(kind=kind, name=sd.get("name", ""),
                                     params=dict(sd.get("params", {})), transform=t,
                                     visible=bool(sd.get("visible", True)),
                                     z=int(sd.get("z", 0))))
        return sc


# --- Proyecto = coleccion de escenas -------------------------------------
def collection_to_dict(scenes: list[Scene], active: int = 0) -> dict:
    """Serializa varias escenas como un proyecto: {"scenes": [...], "active": i}."""
    return {"scenes": [s.to_dict() for s in scenes], "active": int(active)}


def scenes_from_data(data: dict) -> list[Scene]:
    """Lee un proyecto-coleccion o, por compatibilidad, una escena suelta antigua."""
    if isinstance(data, dict) and "scenes" in data:
        return [Scene.from_dict(d) for d in data["scenes"]]
    if isinstance(data, dict) and "sources" in data:   # formato antiguo (1 escena)
        return [Scene.from_dict(data)]
    raise ValueError("formato de escena no reconocido")


# --- Fabricas de fuentes comunes -----------------------------------------
def screen_source(region: tuple[int, int, int, int], name: str = "Pantalla") -> Source:
    left, top, w, h = region
    return Source(kind=KIND_SCREEN, name=name,
                  params={"left": left, "top": top, "width": w, "height": h},
                  transform=Transform(x=0, y=0))


def window_source(title: str, name: str = "") -> Source:
    """Captura de UNA ventana por su titulo (gdigrab -i title=). El recorte para
    quitar barras/pestanas se guarda en transform.crop."""
    return Source(kind=KIND_WINDOW, name=name or title,
                  params={"title": title}, transform=Transform(x=0, y=0))


def webcam_source(device: str, x: int, y: int, size: int = 360,
                  circle: bool = True) -> Source:
    return Source(kind=KIND_WEBCAM, name="Webcam",
                  params={"device": device},
                  transform=Transform(x=x, y=y, w=size, h=size,
                                      shape="circle" if circle else "rect"))


def image_source(path: str, x: int = 40, y: int = 40, w: int = 0) -> Source:
    return Source(kind=KIND_IMAGE, name="Imagen", params={"path": path},
                  transform=Transform(x=x, y=y, w=w))


def text_source(text: str, x: int = 60, y: int = 60, size: int = 48,
                color: str = "#FFFFFF", bg: str | None = "#1E3A5F") -> Source:
    return Source(kind=KIND_TEXT, name="Texto",
                  params={"text": text, "size": size, "color": color, "bg": bg},
                  transform=Transform(x=x, y=y))


def color_source(color: str = "#1E3A5F", w: int = 1920, h: int = 1080) -> Source:
    return Source(kind=KIND_COLOR, name="Fondo", params={"color": color},
                  transform=Transform(x=0, y=0, w=w, h=h))
