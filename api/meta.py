"""Metadatos e interfaz de runtime que el hub AGREGA al <head> de un artifact.

Por que aca y no en nginx: nginx sirve los artifacts como estaticos, que es lo que los hace
rapidos y robustos, y su `sub_filter` no interpola variables en el reemplazo. Inyectar al
publicar es deterministico, queda dentro del snapshot inmutable y se prueba con un unit test.

Tres reglas que no se negocian:

  * **Lo que el autor ya declaro GANA.** Si la pagina trae su propio `og:image` o
    `og:title`, no lo pisamos: se respeta.
  * **Sin `<head>` no se toca nada.** Un fragmento HTML o un documento raro se guarda
    exactamente como vino; preferimos un artifact sin vista previa a uno roto.
  * **Se puede reconocer.** Todo lo que agregamos va precedido por MARK, asi se ve de un
    vistazo que esas etiquetas no las escribio el autor.

Lo que habilita:

  * `og:*` — el link se ve bien al pegarlo en cualquier chat (Telegram, Slack, WhatsApp),
    no solo dentro de la herramienta que lo genero.
  * `window.ARTIFACT` — la pagina sabe con que hub habla y con que clave, sin que el autor
    tenga que averiguar la URL ni pegar un secreto a mano.
"""
import re

MARK = b"<!-- artifact-craft:meta v1 -->"
RUNTIME_MARK = b"<!-- artifact-craft:runtime v2 -->"


def _load_runtime():
    """runtime.js sin comentarios: va inline en CADA pagina, asi que lo que no ejecuta no viaja.

    Solo se quitan bloques /* */ y lineas que son enteras un comentario //, nunca nada dentro
    de una linea de codigo (ahi puede haber "//" en un string). Si el archivo falta, la imagen
    esta mal armada y el hub NO arranca: mejor eso que paginas sin runtime en silencio.
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent / "runtime.js").read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    lines = [l.strip() for l in src.splitlines()]
    out = "\n".join(l for l in lines if l and not l.startswith("//"))
    if "</" in out or "<!--" in out:
        raise RuntimeError("runtime.js no puede contener '</' ni '<!--': cerraria el <script>")
    return out.encode("utf-8")


RUNTIME_JS = _load_runtime()

_HEAD_OPEN = re.compile(rb"<head\b[^>]*>", re.I)
_HEAD_CLOSE = re.compile(rb"</head\s*>", re.I)
_MAX_SCOPE = 65536  # cuanto del <head> miramos para saber que ya declaro el autor


def esc_attr(s):
    """Escapa para un atributo entrecomillado. El texto viene del usuario."""
    return (str(s or "").replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _meta(prop, content):
    return b'<meta property="' + prop.encode() + b'" content="' + esc_attr(content).encode() + b'">'


def _meta_name(name, content):
    return b'<meta name="' + name.encode() + b'" content="' + esc_attr(content).encode() + b'">'


def head_of(body):
    """Devuelve (inicio, fin) del contenido del <head>, o None si no hay <head>."""
    m = _HEAD_OPEN.search(body)
    if not m:
        return None
    close = _HEAD_CLOSE.search(body, m.end())
    end = close.start() if close else min(len(body), m.end() + _MAX_SCOPE)
    return m.end(), end


def inject(body, *, title=None, description=None, url=None, image=None,
           site=None, runtime=None):
    """Devuelve el cuerpo con la vista previa (y el runtime) agregados al <head>.

    Nunca falla: ante cualquier duda devuelve el cuerpo original sin tocar.
    """
    if not body or len(body) > 8 * 1024 * 1024:
        return body
    span = head_of(body)
    if span is None:
        return body
    start, end = span
    low = body[start:end].lower()

    tags = []
    if title and b"og:title" not in low:
        tags.append(_meta("og:title", title))
    if url and b"og:url" not in low:
        tags.append(_meta("og:url", url))
    if site and b"og:site_name" not in low:
        tags.append(_meta("og:site_name", site))
    if b"og:type" not in low:
        tags.append(_meta("og:type", "website"))
    if description and b"og:description" not in low:
        tags.append(_meta("og:description", description))
    if image and b"og:image" not in low:
        tags.append(_meta("og:image", image))
        tags.append(_meta("og:image:width", "1200"))
        tags.append(_meta("og:image:height", "630"))
        if title:
            tags.append(_meta("og:image:alt", title))
    if image and b"twitter:card" not in low:
        tags.append(_meta_name("twitter:card", "summary_large_image"))

    chunks = []
    if tags:
        chunks.append(MARK + b"\n" + b"\n".join(tags))
    if runtime:
        # La clave va aca a proposito: el artifact ES el portador del link, y la clave
        # solo alcanza a ESTE artifact. Nunca es el token de la persona.
        payload = ("window.ARTIFACT=" + _json(runtime) + ";").encode()
        chunks.append(RUNTIME_MARK + b"\n<script>" + payload + b"\n" + RUNTIME_JS + b"</script>")

    if not chunks:
        return body
    block = b"\n".join(chunks) + b"\n"
    return body[:start] + b"\n" + block + body[start:]


def _json(obj):
    """json.dumps compacto, sin depender de json aca adentro para el caso chico."""
    import json
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True)


def description_from(body, limit=300):
    """La descripcion que el autor ya escribio, si la escribio.

    Se prefiere siempre lo declarado en la pagina antes que inventar un texto: una vista
    previa que promete algo que el artifact no dice es peor que una sin descripcion.
    """
    span = head_of(body)
    if span is None:
        return None
    scope = body[span[0]:span[1]].decode("utf-8", "replace")
    m = re.search(r'<meta[^>]+name=["\']description["\'][^>]*>', scope, re.I)
    if not m:
        m = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]*>', scope, re.I)
    if not m:
        return None
    c = re.search(r'content=["\'](.*?)["\']', m.group(0), re.I | re.S)
    if not c:
        return None
    text = re.sub(r"\s+", " ", c.group(1)).strip()
    return text[:limit] or None