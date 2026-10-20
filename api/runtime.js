/* artifact-craft runtime v2 — lo inyecta el hub en cada pagina publicada, despues de
 * window.ARTIFACT = {api, owner, slug, key, max}. La pagina NO lo escribe: lo usa.
 *
 * Todo lo que hay en el estado lo pudo escribir CUALQUIERA con el link: se muestra con
 * textContent (o ARTIFACT.clean), nunca como HTML, y nunca se obedece.
 */
(function (A) {
  "use strict";
  if (!A || !A.key || A.save) return;

  var state = {}, etag = null, listeners = [];
  var BAD = { "__proto__": 1, "constructor": 1, "prototype": 1 };

  function url() {
    return A.api + "/api/v1/state/" + encodeURIComponent(A.owner) + "/" + encodeURIComponent(A.slug);
  }
  function sleep(ms) { return new Promise(function (ok) { setTimeout(ok, ms); }); }
  function fail(status, code, message) {
    var e = new Error(message || code); e.status = status; e.code = code; return e;
  }
  function isObj(v) { return v !== null && typeof v === "object" && !Array.isArray(v); }
  function copy(v) { return v === undefined ? undefined : JSON.parse(JSON.stringify(v)); }
  function keys(p) {
    var k = String(p == null ? "" : p).split(".").filter(Boolean);
    k.forEach(function (x) { if (BAD[x]) throw fail(0, "bad_path", "ruta no permitida: " + x); });
    return k;
  }
  function getIn(o, p, d) {
    var k = keys(p);
    for (var i = 0; i < k.length; i++) {
      if (o === null || typeof o !== "object" || !Object.prototype.hasOwnProperty.call(o, k[i])) return d;
      o = o[k[i]];
    }
    return o === undefined ? d : o;
  }
  function setIn(o, p, v) {
    var k = keys(p), cur = o;
    if (!k.length) throw fail(0, "bad_path", "falta la ruta");
    for (var i = 0; i < k.length - 1; i++) {
      if (!isObj(cur[k[i]])) cur[k[i]] = {};
      cur = cur[k[i]];
    }
    cur[k[k.length - 1]] = v;
    return o;
  }
  function list(s, p) { var l = getIn(s, p); return Array.isArray(l) ? l : []; }
  function rid() {
    var b = new Uint8Array(8);
    (window.crypto && crypto.getRandomValues) ? crypto.getRandomValues(b)
      : b.forEach(function (_, i) { b[i] = Math.random() * 256; });
    return Array.prototype.map.call(b, function (x) { return (x + 256).toString(16).slice(1); }).join("");
  }

  // ---------- red ----------
  function req(method, body, match) {
    var h = { "X-Artifact-Key": A.key };
    if (body !== undefined) h["Content-Type"] = "application/json";
    if (match) h["If-Match"] = match;
    return fetch(url(), { method: method, headers: h, cache: "no-store",
                          body: body === undefined ? undefined : JSON.stringify(body) });
  }
  function json(r) { return r.json().catch(function () { return {}; }); }
  function remember(s, tag) {
    var changed = tag !== etag;
    state = isObj(s) ? s : {};
    etag = tag || etag;
    if (changed) listeners.slice().forEach(function (f) { try { f(copy(state)); } catch (e) {} });
  }

  // ---------- lo basico ----------
  A.version = 2;
  A.live = true;

  /** Lee el estado del hub. Devuelve una copia ({} si nunca se guardo nada). */
  A.load = async function () {
    for (var i = 0; ; i++) {
      var r = await req("GET");
      if (r.status === 429 && i < 4) { await sleep(500 * Math.pow(2, i) + Math.random() * 500); continue; }
      var b = await json(r);
      if (!r.ok) throw fail(r.status, b.error || "http_" + r.status, b.message);
      remember(b, r.headers.get("ETag"));
      return copy(state);
    }
  };

  /** Cambia el estado SIN pisar a nadie. `change` recibe una copia del estado actual y
   *  devuelve el nuevo, o lo modifica y no devuelve nada. Si alguien guardo en el medio, se
   *  vuelve a correr sobre lo de el: por eso es una funcion y no un valor fijo. */
  A.save = async function (change) {
    if (typeof change !== "function") throw fail(0, "bad_change", "save() recibe una funcion: estado => estado");
    if (etag === null) await A.load();
    for (var i = 0; i < 10; i++) {
      var draft = copy(state), next = change(draft);
      if (next === undefined) next = draft;
      if (!isObj(next)) throw fail(0, "bad_state", "el estado tiene que ser un objeto");
      if (A.max && new TextEncoder().encode(JSON.stringify(next)).length > A.max)
        throw fail(413, "too_large", "el estado supera " + A.max + " bytes: recorta listas viejas");
      var r = await req("PUT", next, etag), b = await json(r);
      if (r.ok) { remember(next, r.headers.get("ETag") || b.etag); return copy(state); }
      if (r.status === 412) remember(b.state, b.etag);        // otro guardo: se mezcla sobre lo suyo
      else if (r.status !== 429) throw fail(r.status, b.error || "http_" + r.status, b.message);
      await sleep(Math.random() * Math.min(2000, 100 * Math.pow(2, i)) + (r.status === 429 ? 1000 : 0));
    }
    throw fail(409, "busy", "demasiada gente guardando a la vez: reintenta");
  };

  /** Avisa cuando el estado cambia (lo cambie quien lo cambie). Llama una vez al empezar.
   *  Consulta solo con la pagina visible, y espera mas si el hub pide calma. Devuelve stop(). */
  A.watch = function (callback, opts) {
    var every = Math.max(3000, (opts && opts.every) || 15000), wait = every, stopped = false, t = null;
    listeners.push(callback);
    async function tick() {
      if (stopped) return;
      if (!(typeof document !== "undefined" && document.hidden)) {
        try { await A.load(); wait = every; }
        catch (e) { wait = Math.min(wait * 2, 120000); }
      }
      if (!stopped) t = setTimeout(tick, wait);
    }
    if (etag !== null) { try { callback(copy(state)); } catch (e) {} }
    tick();
    return function stop() {
      stopped = true; clearTimeout(t);
      listeners = listeners.filter(function (f) { return f !== callback; });
    };
  };

  /** Valor de una ruta ("a.b.c") en lo ultimo que se leyo. Sin ruta, todo el estado. */
  A.get = function (path, fallback) { return copy(path == null ? state : getIn(state, path, fallback)); };
  A.set = function (path, value) { return A.save(function (s) { setIn(s, path, copy(value)); }); };

  /** Suma (o resta) a un numero. Devuelve el valor nuevo. */
  A.incr = async function (path, by) {
    var s = await A.save(function (s) { setIn(s, path, (Number(getIn(s, path, 0)) || 0) + (by == null ? 1 : by)); });
    return getIn(s, path);
  };

  // ---------- listas: comentarios, tareas, respuestas, libro de visitas ----------
  /** Agrega un item con id, fecha y visitante. Con `max`, se quedan los ultimos. */
  A.push = async function (path, item, opts) {
    var max = (opts && opts.max) || 200, entry = null;
    await A.save(function (s) {
      entry = Object.assign({ id: rid(), at: Date.now(), by: A.visitor }, copy(item));
      setIn(s, path, list(s, path).concat([entry]).slice(-max));
    });
    return entry;
  };
  A.update = function (path, id, patch) {
    return A.save(function (s) {
      setIn(s, path, list(s, path).map(function (x) {
        return x && x.id === id ? Object.assign({}, x, copy(patch), { id: id }) : x;
      }));
    });
  };
  A.remove = function (path, id) {
    return A.save(function (s) { setIn(s, path, list(s, path).filter(function (x) { return !x || x.id !== id; })); });
  };

  // ---------- votos: uno por visitante, se puede cambiar ----------
  /** Vota `option` en la encuesta `poll`. Con {multi:true} cada opcion se prende y apaga.
   *  Devuelve el recuento. Es uno por NAVEGADOR, no por persona: no hay login. */
  A.vote = async function (poll, option, opts) {
    var multi = !!(opts && opts.multi), me = A.visitor, o = String(option);
    var s = await A.save(function (s) {
      var votes = getIn(s, poll + ".votes");
      if (!isObj(votes)) votes = {};
      if (multi) {
        var mine = Array.isArray(votes[me]) ? votes[me] : [];
        votes[me] = mine.indexOf(o) >= 0 ? mine.filter(function (x) { return x !== o; }) : mine.concat([o]);
      } else votes[me] = o;
      setIn(s, poll + ".votes", votes);
    });
    return A.tally(poll, s);
  };
  /** {opcion: votos, ...} mas `_total` (votantes) y `_mine` (lo que voto este navegador). */
  A.tally = function (poll, from) {
    var votes = getIn(from || state, poll + ".votes"), out = { _total: 0, _mine: null };
    if (!isObj(votes)) return out;
    Object.keys(votes).forEach(function (v) {
      var picks = Array.isArray(votes[v]) ? votes[v] : [votes[v]];
      picks = picks.filter(function (p) { return typeof p === "string" && p; });
      if (!picks.length) return;
      out._total++;
      picks.forEach(function (p) { if (!BAD[p] && p[0] !== "_") out[p] = (out[p] || 0) + 1; });
    });
    out._mine = votes[A.visitor] === undefined ? null : copy(votes[A.visitor]);
    return out;
  };

  // ---------- rankings ----------
  /** Inserta `entry` en el ranking `board`, ordenado por `by` (por defecto "score").
   *  {max:20, order:"desc", best:true} — best: un lugar por visitante, su mejor marca.
   *  Devuelve {position (1..max, o 0 si no entro), entry, board}. */
  A.rank = async function (board, entry, opts) {
    var o = opts || {}, by = o.by || "score", max = o.max || 20, desc = o.order !== "asc",
        best = o.best !== false, mine = null;
    function num(x) { var n = Number(x && x[by]); return isFinite(n) ? n : null; }
    var s = await A.save(function (s) {
      mine = Object.assign({ id: rid(), at: Date.now(), by: A.visitor }, copy(entry));
      var l = list(s, board).filter(function (x) { return x && num(x) !== null; });
      if (best) {
        var prev = l.filter(function (x) { return x.by === A.visitor; })[0];
        if (prev && (desc ? num(prev) >= num(mine) : num(prev) <= num(mine))) { mine = prev; return; }
        l = l.filter(function (x) { return x.by !== A.visitor; });
      }
      l.push(mine);
      l.sort(function (a, b) { return (desc ? num(b) - num(a) : num(a) - num(b)) || a.at - b.at; });
      setIn(s, board, l.slice(0, max));
    });
    var l2 = list(s, board), pos = 0;
    l2.forEach(function (x, i) { if (x && x.id === mine.id) pos = i + 1; });
    return { position: pos, entry: copy(mine), board: copy(l2) };
  };

  // ---------- por visitante (en este navegador) ----------
  var PFX = "artifact:" + A.owner + "/" + A.slug + ":";
  /** Preferencias de ESTE visitante: tema, ultimo nombre, una seccion plegada. No es privado
   *  ni seguro (el navegador lo comparte con otras paginas del hub): solo comodidades. */
  A.local = {
    get: function (k, d) {
      try { var v = localStorage.getItem(PFX + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; }
    },
    set: function (k, v) { try { localStorage.setItem(PFX + k, JSON.stringify(v)); } catch (e) {} },
    remove: function (k) { try { localStorage.removeItem(PFX + k); } catch (e) {} }
  };
  /** Id anonimo y estable de este navegador. Sirve para "lo mio" y un-voto-por-navegador. */
  A.visitor = A.local.get("visitor") || (function () { var v = "v_" + rid(); A.local.set("visitor", v); return v; })();

  // ---------- IA: el hub llama al modelo con SU clave; la pagina nunca la ve ----------
  function aiUrl() {
    return A.api + "/api/v1/ai/" + encodeURIComponent(A.owner) + "/" + encodeURIComponent(A.slug);
  }
  function aiBody(input, opts) {
    var b;
    if (typeof input === "string") b = { prompt: input };
    else if (isObj(input)) b = copy(input);
    else throw fail(0, "bad_input", "A.ai recibe un texto o {system, messages, prompt}");
    return Object.assign(b, copy(opts || {}));
  }
  async function aiPost(body) {
    var r = await fetch(aiUrl(), { method: "POST", cache: "no-store", body: JSON.stringify(body),
      headers: { "X-Artifact-Key": A.key, "Content-Type": "application/json" } });
    if (!r.ok) {
      var b = await json(r), e = fail(r.status, b.error || "http_" + r.status, b.message);
      if (b.retry_after) e.retryAfter = b.retry_after;
      throw e;
    }
    return r;
  }
  function looseJson(text) {
    var t = String(text || "").trim().replace(/^```(?:json)?\s*/i, "").replace(/```$/, "").trim();
    try { return JSON.parse(t); } catch (e) {}
    var a = t.indexOf("{"), z = t.lastIndexOf("}");
    if (a >= 0 && z > a) { try { return JSON.parse(t.slice(a, z + 1)); } catch (e) {} }
    throw fail(502, "ai_bad_json", "la IA no devolvio un JSON valido");
  }

  /** Le pide algo al modelo y devuelve el texto. `input`: un texto, o {system, messages,
   *  prompt}. `opts`: {model, max_tokens, temperature}. */
  A.ai = async function (input, opts) {
    var r = await aiPost(aiBody(input, opts));
    return (await r.json()).text || "";
  };
  /** Igual, pero llega en vivo: onText(pedazo, textoHastaAhora) por cada pedazo. Devuelve el
   *  texto completo. Para que el visitante vea la respuesta escribirse. */
  A.ai.stream = async function (input, onText, opts) {
    var body = aiBody(input, opts); body.stream = true;
    var r = await aiPost(body), full = "", buf = "";
    function handle(chunk) {
      buf += chunk;
      var parts = buf.split("\n\n"); buf = parts.pop();
      parts.forEach(function (p) {
        if (p.indexOf("data:") !== 0) return;
        var ev; try { ev = JSON.parse(p.slice(5)); } catch (e) { return; }
        if (ev.error) throw fail(502, ev.error, ev.message);
        if (ev.t) { full += ev.t; if (onText) onText(ev.t, full); }
      });
    }
    if (r.body && r.body.getReader) {
      var reader = r.body.getReader(), dec = new TextDecoder();
      for (;;) { var x = await reader.read(); if (x.done) break; handle(dec.decode(x.value, { stream: true })); }
    } else handle(await r.text());
    handle("\n\n");
    return full;
  };
  /** {available, model, models, per_visitor_hour, per_page_day}: si esta pagina tiene IA y con
   *  que modelos. No gasta cupo. Para mostrar "IA no disponible" antes de que el visitante escriba. */
  A.ai.info = async function () {
    var r = await fetch(aiUrl(), { headers: { "X-Artifact-Key": A.key }, cache: "no-store" }), b = await json(r);
    if (!r.ok) throw fail(r.status, b.error || "http_" + r.status, b.message);
    delete b.ok;
    return b;
  };
  /** Devuelve un OBJETO: para generadores (un pitch con partes, opciones, una clasificacion).
   *  Decile en el prompt que forma queres: "{titulo, puntos: [..], cierre}". */
  A.ai.json = async function (input, opts) {
    var body = aiBody(input, opts); body.json = true;
    var r = await aiPost(body);
    return looseJson((await r.json()).text);
  };
  /** Una conversacion que recuerda lo dicho (tipo ChatGPT). {system, model, keep: 20}.
   *  c.send(texto, onText?) -> respuesta · c.history · c.reset(). */
  A.ai.chat = function (opts) {
    var o = opts || {}, hist = [];
    return {
      history: hist,
      send: async function (text, onText) {
        hist.push({ role: "user", content: String(text) });
        var input = { system: o.system, messages: hist.slice(-(o.keep || 20)) };
        var extra = { model: o.model, max_tokens: o.max_tokens, temperature: o.temperature };
        Object.keys(extra).forEach(function (k) { if (extra[k] === undefined) delete extra[k]; });
        try {
          var out = onText ? await A.ai.stream(input, onText, extra) : await A.ai(input, extra);
          hist.push({ role: "assistant", content: out });
          return out;
        } catch (e) { hist.pop(); throw e; }
      },
      reset: function () { hist.length = 0; }
    };
  };

  // ---------- markdown seguro: lo que escribe una IA, como DOM y nunca como HTML ----------
  function inline(parent, s) {
    var re = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*\s][^*]*\*|\[[^\]]+\]\(https?:\/\/[^\s)]+\))/g, last = 0, m, e;
    while ((m = re.exec(s))) {
      if (m.index > last) parent.appendChild(document.createTextNode(s.slice(last, m.index)));
      var t = m[0];
      if (t[0] === "`") { e = document.createElement("code"); e.textContent = t.slice(1, -1); }
      else if (t.slice(0, 2) === "**") { e = document.createElement("strong"); inline(e, t.slice(2, -2)); }
      else if (t[0] === "*") { e = document.createElement("em"); inline(e, t.slice(1, -1)); }
      else {
        var k = t.indexOf("](");
        e = document.createElement("a"); e.textContent = t.slice(1, k); e.href = t.slice(k + 2, -1);
        e.target = "_blank"; e.rel = "noopener noreferrer";
      }
      parent.appendChild(e); last = re.lastIndex;
    }
    if (last < s.length) parent.appendChild(document.createTextNode(s.slice(last)));
  }
  var LIST = /^\s*([-*\u2022]|\d+[.)])\s+/;
  /** Pinta `text` (markdown: titulos, listas, **negrita**, *italica*, `codigo`, bloques ```,
   *  links http) dentro de `target`, reemplazando lo que habia. Sin innerHTML: un <script> en
   *  la respuesta queda como texto. Llamalo en cada pedazo de A.ai.stream para verlo crecer. */
  A.md = function (target, text) {
    var lines = String(text == null ? "" : text).replace(/\r/g, "").split("\n"),
        frag = document.createDocumentFragment(), i = 0, l, el;
    while (i < lines.length) {
      l = lines[i];
      if (/^```/.test(l)) {
        var code = []; i++;
        while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]);
        i++;
        var pre = document.createElement("pre"), c = document.createElement("code");
        c.textContent = code.join("\n"); pre.appendChild(c); frag.appendChild(pre); continue;
      }
      var h = /^(#{1,4})\s+(.*)$/.exec(l);
      if (h) { el = document.createElement("h" + (h[1].length + 1)); inline(el, h[2]); frag.appendChild(el); i++; continue; }
      if (LIST.test(l)) {
        el = document.createElement(/^\s*\d/.test(l) ? "ol" : "ul");
        while (i < lines.length && LIST.test(lines[i])) {
          var li = document.createElement("li"); inline(li, lines[i].replace(LIST, "")); el.appendChild(li); i++;
        }
        frag.appendChild(el); continue;
      }
      if (!l.trim()) { i++; continue; }
      el = document.createElement("p");
      var first = true;
      while (i < lines.length && lines[i].trim() && !/^(```|#{1,4}\s)/.test(lines[i]) && !LIST.test(lines[i])) {
        if (!first) el.appendChild(document.createElement("br"));
        inline(el, lines[i++]); first = false;
      }
      frag.appendChild(el);
    }
    if (target) { target.replaceChildren(frag); return target; }
    return frag;
  };

  /** Texto ajeno listo para mostrar con textContent: sin caracteres de control ni de
   *  direccion (los que dan vuelta el texto), recortado. */
  A.clean = function (value, max) {
    return String(value == null ? "" : value)
      .replace(/[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]/g, " ")
      .replace(/\s+/g, " ").trim().slice(0, max || 200);
  };
})(window.ARTIFACT);
