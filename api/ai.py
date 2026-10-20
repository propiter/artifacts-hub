"""IA para las paginas publicadas: el hub es el intermediario y el UNICO que tiene la clave.

Una pagina publicada es publica: una clave de API escrita en ella la usa cualquiera. Por eso la
pagina llama al hub con SU clave de artifact (la misma del estado) y el hub llama al proveedor
compatible con OpenAI con la clave del servidor. Lo que el hub controla aca:

  * que modelo (solo los permitidos), cuantos tokens de salida (con techo), cuanto texto entra;
  * los cupos por visitante y por pagina los aplica app.py antes de llamar a esto;
  * la clave no sale nunca: ni en la pagina, ni en un error, ni en un log.
"""
import json
import re
import urllib.error
import urllib.request

ROLES = ("system", "user", "assistant")


class AiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def enabled(cfg):
    return bool(cfg.get("ai_key") and cfg.get("ai_base_url") and cfg.get("ai_model"))


def models(cfg):
    extra = [m.strip() for m in (cfg.get("ai_models") or "").split(",") if m.strip()]
    return [cfg["ai_model"]] + [m for m in extra if m != cfg["ai_model"]]


def build(cfg, body):
    """Valida lo que pide la pagina y arma el pedido al proveedor. Devuelve (payload, stream)."""
    if not isinstance(body, dict):
        raise AiError(400, "bad_input", "el pedido es un objeto: {prompt} o {system, messages}")
    msgs = []
    if body.get("system") is not None:
        if not isinstance(body["system"], str):
            raise AiError(400, "bad_input", "system tiene que ser texto")
        if body["system"].strip():
            msgs.append({"role": "system", "content": body["system"]})
    if body.get("messages") is not None:
        if not isinstance(body["messages"], list):
            raise AiError(400, "bad_messages", "messages es una lista")
        for m in body["messages"]:
            if (not isinstance(m, dict) or m.get("role") not in ROLES
                    or not isinstance(m.get("content"), str)):
                raise AiError(400, "bad_messages",
                              "cada mensaje es {role: system|user|assistant, content: texto}")
            msgs.append({"role": m["role"], "content": m["content"]})
    prompt = body.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        msgs.append({"role": "user", "content": prompt})
    if not any(m["role"] == "user" and m["content"].strip() for m in msgs):
        raise AiError(400, "empty", "falta lo que se le pide a la IA (prompt, o un mensaje user)")
    if len(msgs) > cfg["ai_max_messages"]:
        raise AiError(413, "too_large", "demasiados mensajes (max %d): recorta el historial"
                      % cfg["ai_max_messages"])
    if sum(len(m["content"]) for m in msgs) > cfg["ai_max_input_chars"]:
        raise AiError(413, "too_large", "el pedido supera %d caracteres: resumi o recorta"
                      % cfg["ai_max_input_chars"])

    model = body.get("model") or cfg["ai_model"]
    if model not in models(cfg):
        raise AiError(400, "bad_model", "modelo no permitido; usa uno de: " + ", ".join(models(cfg)))

    cap = cfg["ai_max_tokens"]
    asked = body.get("max_tokens")
    out_tokens = min(asked, cap) if isinstance(asked, int) and not isinstance(asked, bool) and asked > 0 else cap
    payload = {"model": model, "messages": msgs, cfg["ai_tokens_param"]: out_tokens}

    t = body.get("temperature")
    if isinstance(t, (int, float)) and not isinstance(t, bool):
        payload["temperature"] = max(0.0, min(2.0, float(t)))
    if body.get("json"):
        payload["response_format"] = {"type": "json_object"}
        # el modo JSON del proveedor exige que el pedido diga "JSON"
        if not any("json" in m["content"].lower() for m in msgs):
            msgs.insert(0, {"role": "system", "content": "Respond only with one valid JSON object."})
    stream = bool(body.get("stream"))
    if stream:
        payload["stream"] = True
    return payload, stream


def _request(cfg, payload):
    return urllib.request.Request(
        cfg["ai_base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + cfg["ai_key"], "Content-Type": "application/json",
                 "Accept": "text/event-stream" if payload.get("stream") else "application/json"},
        method="POST")


_TOKENISH = re.compile(r"[A-Za-z0-9_\-.]{24,}")


def _upstream_message(cfg, raw):
    """El mensaje del proveedor, corto, sin la clave y sin nada que se le parezca."""
    try:
        err = json.loads(raw or b"{}").get("error")
        msg = err.get("message") if isinstance(err, dict) else str(err or "")
    except Exception:
        msg = ""
    msg = str(msg or "el proveedor de IA respondio con error")
    if cfg.get("ai_key"):
        msg = msg.replace(cfg["ai_key"], "[clave]")
    return _TOKENISH.sub("[...]", msg)[:300]


def _open(cfg, payload):
    try:
        return urllib.request.urlopen(_request(cfg, payload), timeout=cfg["ai_timeout"])
    except urllib.error.HTTPError as e:
        status = 503 if e.code == 429 else 502
        raise AiError(status, "ai_upstream", _upstream_message(cfg, e.read()))
    except (urllib.error.URLError, TimeoutError, OSError):
        raise AiError(504, "ai_timeout", "el proveedor de IA no respondio a tiempo")


def complete(cfg, payload):
    resp = _open(cfg, payload)
    try:
        d = json.loads(resp.read() or b"{}")
    except ValueError:
        raise AiError(502, "ai_upstream", "el proveedor de IA devolvio algo que no es JSON")
    finally:
        resp.close()
    ch = (d.get("choices") or [{}])[0] or {}
    text = (ch.get("message") or {}).get("content") or ""
    return {"text": text, "model": d.get("model") or payload["model"], "usage": d.get("usage") or {}}


def open_stream(cfg, payload):
    return _open(cfg, payload)


def iter_stream(cfg, resp):
    """Los pedazos de texto de un stream SSE de chat/completions."""
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            ev = json.loads(data)
        except ValueError:
            continue
        if ev.get("error"):
            raise AiError(502, "ai_upstream", _upstream_message(cfg, json.dumps(ev).encode()))
        for ch in ev.get("choices") or []:
            piece = (ch.get("delta") or {}).get("content")
            if piece:
                yield piece
