// Escenarios del runtime que el hub inyecta, corridos con Node contra un hub REAL.
// Uso: node tests/runtime_scenarios.mjs <runtime-tal-cual-se-sirve.js> <url-del-hub>
//
// Cada "visitante" es un contexto aislado (su propio window, su localStorage, su ARTIFACT):
// lo mismo que dos navegadores distintos abriendo el link.
import { readFileSync } from "node:fs";
import vm from "node:vm";
import assert from "node:assert/strict";

const [, , snippetPath, base] = process.argv;
const snippet = readFileSync(snippetPath, "utf8");

// Un DOM minimo: lo justo para A.md (crear nodos, anidarlos, leerlos de vuelta). Si A.md
// usara innerHTML, este DOM no lo interpretaria y el test lo veria.
function miniDom() {
  class Node {
    constructor(tag, text) { this.tag = tag; this.children = []; this.attrs = {}; this._text = text ?? ""; }
    appendChild(c) { if (c.tag === "#frag") this.children.push(...c.children); else this.children.push(c); return c; }
    append(...cs) { cs.forEach(c => this.appendChild(c)); }
    replaceChildren(...cs) { this.children = []; this.append(...cs); }
    set textContent(v) { this.children = [new Node("#text", String(v))]; }
    get textContent() { return this.tag === "#text" ? this._text : this.children.map(c => c.textContent).join(""); }
    set innerHTML(v) { throw new Error("innerHTML usado"); }
    set href(v) { this.attrs.href = v; } get href() { return this.attrs.href; }
    set target(v) { this.attrs.target = v; } set rel(v) { this.attrs.rel = v; }
    shape() { return this.tag === "#text" ? this._text : { [this.tag]: this.children.map(c => c.shape()) }; }
  }
  return { hidden: false, createElement: t => new Node(t), createTextNode: t => new Node("#text", t),
           createDocumentFragment: () => new Node("#frag") };
}

function visitor({ key } = {}) {
  const store = new Map();
  const doc = miniDom();
  const ctx = {
    fetch, setTimeout, clearTimeout, console, crypto, TextEncoder, TextDecoder, Promise, JSON, Math, Date,
    document: doc,
    localStorage: {
      getItem: k => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: k => store.delete(k),
    },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(snippet, ctx);
  const A = ctx.window.ARTIFACT;
  A.api = base;                       // la pagina publicada apunta al hub publico; aca, al de prueba
  if (key) A.key = key;
  A._doc = doc;
  return A;
}

const results = [];
async function scenario(name, fn) {
  try { await fn(); results.push("ok   " + name); }
  catch (e) { results.push("FAIL " + name + "\n     " + (e.stack || e).toString().split("\n").slice(0, 3).join("\n     ")); }
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

await scenario("expone la API y arranca vivo", async () => {
  const A = visitor();
  assert.equal(A.live, true);
  assert.equal(A.version, 2);
  for (const f of ["load", "save", "watch", "get", "set", "incr", "push", "update", "remove",
                   "vote", "tally", "rank", "clean"]) assert.equal(typeof A[f], "function", f);
  assert.match(A.visitor, /^v_[0-9a-f]{16}$/);
  assert.equal(typeof A.max, "number");
});

await scenario("sin nada guardado, load devuelve {}", async () => {
  assert.deepEqual(await visitor().load(), {});
});

await scenario("save acepta devolver el estado o modificarlo", async () => {
  const A = visitor();
  await A.save(s => ({ ...s, a: 1 }));
  const s = await A.save(s => { s.b = 2; });
  assert.equal(s.a, 1); assert.equal(s.b, 2);
  assert.equal(A.get("b"), 2);
});

await scenario("diez visitantes a la vez: ninguno se pierde", async () => {
  const vs = Array.from({ length: 10 }, () => visitor());
  await Promise.all(vs.map(v => v.incr("carrera.contador")));
  assert.equal((await visitor().load()).carrera.contador, 10);
});

await scenario("set e incr con rutas", async () => {
  const A = visitor();
  await A.set("config.tema", "oscuro");
  assert.equal(await A.incr("config.vistas", 5), 5);
  assert.equal(await A.incr("config.vistas", -2), 3);
  assert.equal(A.get("config.tema"), "oscuro");
  assert.equal(A.get("no.existe", "x"), "x");
});

await scenario("push agrega id, fecha y visitante, y respeta max", async () => {
  const A = visitor();
  for (let i = 0; i < 5; i++) await A.push("libro", { texto: "hola " + i }, { max: 3 });
  const l = A.get("libro");
  assert.equal(l.length, 3);
  assert.deepEqual(l.map(x => x.texto), ["hola 2", "hola 3", "hola 4"]);
  for (const x of l) { assert.match(x.id, /^[0-9a-f]{16}$/); assert.equal(x.by, A.visitor); assert.ok(x.at > 0); }
});

await scenario("update y remove por id", async () => {
  const A = visitor();
  const t = await A.push("tareas", { texto: "comprar pan", hecha: false });
  await A.push("tareas", { texto: "llamar", hecha: false });
  await A.update("tareas", t.id, { hecha: true, id: "no-me-cambies-el-id" });
  assert.deepEqual(A.get("tareas").map(x => [x.texto, x.hecha]), [["comprar pan", true], ["llamar", false]]);
  assert.equal(A.get("tareas")[0].id, t.id);
  await A.remove("tareas", t.id);
  assert.deepEqual(A.get("tareas").map(x => x.texto), ["llamar"]);
});

await scenario("votos: uno por visitante, se puede cambiar", async () => {
  const [a, b, c] = [visitor(), visitor(), visitor()];
  await a.vote("almuerzo", "pizza");
  await b.vote("almuerzo", "sushi");
  await c.vote("almuerzo", "pizza");
  await c.vote("almuerzo", "pizza");                  // repetir no suma
  let t = await a.vote("almuerzo", "sushi");          // cambiar de idea
  assert.equal(t.pizza, 1); assert.equal(t.sushi, 2); assert.equal(t._total, 3);
  assert.equal(t._mine, "sushi");
});

await scenario("votos multiples: cada opcion se prende y apaga", async () => {
  const a = visitor();
  await a.vote("dias", "lunes", { multi: true });
  await a.vote("dias", "martes", { multi: true });
  const t = await a.vote("dias", "lunes", { multi: true });
  assert.deepEqual(t._mine, ["martes"]); assert.equal(t.lunes, undefined); assert.equal(t.martes, 1);
});

await scenario("ranking: orden, max, y un lugar por visitante con su mejor marca", async () => {
  const [a, b, c] = [visitor(), visitor(), visitor()];
  await a.rank("top", { name: "ana", score: 50 }, { max: 2 });
  await b.rank("top", { name: "beto", score: 80 }, { max: 2 });
  let r = await c.rank("top", { name: "caro", score: 10 }, { max: 2 });
  assert.equal(r.position, 0, "no entro al top 2");
  r = await a.rank("top", { name: "ana", score: 30 }, { max: 2 });   // peor que su marca
  assert.equal(r.entry.score, 50);
  r = await a.rank("top", { name: "ana", score: 99 }, { max: 2 });   // la mejora
  assert.equal(r.position, 1);
  assert.deepEqual(r.board.map(x => [x.name, x.score]), [["ana", 99], ["beto", 80]]);
});

await scenario("ranking ascendente (tiempos: menos es mejor)", async () => {
  const [a, b] = [visitor(), visitor()];
  await a.rank("tiempos", { ms: 900 }, { by: "ms", order: "asc" });
  const r = await b.rank("tiempos", { ms: 400 }, { by: "ms", order: "asc" });
  assert.equal(r.position, 1);
});

await scenario("watch avisa cuando OTRO cambia el estado, y stop lo corta", async () => {
  const [a, b] = [visitor(), visitor()];
  const vistos = [];
  const stop = a.watch(s => vistos.push(s.pizarra || null), { every: 3000 });
  await sleep(300);
  await b.set("pizarra", "hola desde b");
  await sleep(3400);
  assert.ok(vistos.includes("hola desde b"), "no vio el cambio: " + JSON.stringify(vistos));
  stop();
  const n = vistos.length;
  await b.set("pizarra", "despues de stop");
  await sleep(3400);
  assert.equal(vistos.length, n, "siguio avisando despues de stop()");
});

await scenario("watch no consulta con la pagina oculta", async () => {
  const [a, b] = [visitor(), visitor()];
  const vistos = [];
  const stop = a.watch(s => vistos.push(s.oculta || null), { every: 3000 });
  await sleep(300);
  a._doc.hidden = true;
  await b.set("oculta", "no deberia verse");
  await sleep(3400);
  assert.ok(!vistos.includes("no deberia verse"), "consulto estando oculta");
  stop();
});

await scenario("lo que escribe un extrano no contamina objetos (__proto__)", async () => {
  const A = visitor();
  await fetch(`${base}/api/v1/state/ana/juego`, {
    method: "PUT", headers: { "X-Artifact-Key": A.key, "Content-Type": "application/json" },
    body: '{"__proto__":{"contaminado":1},"ok":1}',
  });
  await A.load();
  assert.equal(({}).contaminado, undefined);
  assert.equal(A.get("ok"), 1);
  await assert.rejects(A.set("__proto__.x", 1), e => e.code === "bad_path");
});

await scenario("clean saca control y direccion, y recorta", async () => {
  const A = visitor();
  assert.equal(A.clean("  ho\u202ela\u0000 mundo \n "), "ho la mundo");
  assert.equal(A.clean("x".repeat(500), 10).length, 10);
  assert.equal(A.clean(null), "");
});

await scenario("local es por pagina y no rompe sin localStorage", async () => {
  const A = visitor();
  A.local.set("nombre", "Pedro");
  assert.equal(A.local.get("nombre"), "Pedro");
  assert.equal(A.local.get("nada", 7), 7);
});

await scenario("errores con codigo: estado gigante y clave invalida", async () => {
  const A = visitor();
  await assert.rejects(A.set("grande", "x".repeat(A.max + 10)), e => e.code === "too_large");
  const malo = visitor({ key: "art_" + "0".repeat(40) });
  await assert.rejects(malo.load(), e => e.status === 401 && e.code === "bad_key");
  await assert.rejects(A.save(5), e => e.code === "bad_change");
});

// ---------- IA (el hub de prueba apunta a un proveedor falso que contesta un eco) ----------
await scenario("A.ai devuelve el texto", async () => {
  const A = visitor();
  assert.equal(await A.ai("hola mundo"), "**eco** (1): hola mundo");
  assert.equal(await A.ai({ system: "Sos breve", prompt: "corto" }, { model: "modelo-chico" }), "**eco** (1): corto");
});

await scenario("A.ai.info dice si hay IA y con que modelos", async () => {
  const i = await visitor().ai.info();
  assert.equal(i.available, true); assert.equal(i.model, "modelo-bueno");
  assert.deepEqual(i.models, ["modelo-bueno", "modelo-chico"]);
});

await scenario("A.ai.stream llega por pedazos y devuelve el total", async () => {
  const A = visitor(), pieces = [];
  const full = await A.ai.stream("uno dos tres cuatro", (t, sofar) => pieces.push(sofar));
  assert.equal(full.trim(), "**eco** (1): uno dos tres cuatro");
  assert.ok(pieces.length > 3, "llego todo junto: " + pieces.length);
  assert.ok(pieces[0].length < pieces[pieces.length - 1].length);
});

await scenario("A.ai.json devuelve un objeto", async () => {
  const o = await visitor().ai.json("armame un pitch con titulo y puntos");
  assert.equal(o.eco, "armame un pitch con titulo y puntos");
});

await scenario("A.ai.chat recuerda la conversacion", async () => {
  const c = visitor().ai.chat({ system: "Sos un asistente" });
  assert.equal(await c.send("primero"), "**eco** (1): primero");
  const out = await c.send("segundo", () => {});           // en vivo, con historial
  assert.equal(out.trim(), "**eco** (2): segundo");
  assert.equal(c.history.length, 4);
  c.reset(); assert.equal(c.history.length, 0);
});

await scenario("errores de IA con codigo, y el chat no guarda el turno fallido", async () => {
  const A = visitor();
  await assert.rejects(A.ai("x", { model: "el-mas-caro" }), e => e.code === "bad_model" && e.status === 400);
  await assert.rejects(A.ai(42), e => e.code === "bad_input");
  const c = A.ai.chat({ model: "el-mas-caro" });
  await assert.rejects(c.send("hola"), e => e.code === "bad_model");
  assert.equal(c.history.length, 0);
});

await scenario("A.md arma DOM y deja el HTML ajeno como texto", async () => {
  const A = visitor(), doc = A._doc;
  const box = doc.createElement("div");
  A.md(box, "# Titulo\n\nHola **mundo** y *algo* con `x()`\n\n- uno\n- dos\n\n1. a\n2. b\n\n```\ncodigo <b>\n```\n\n<img src=x onerror=alert(1)> [link](https://ejemplo.com) [malo](javascript:alert(1))");
  const sh = box.shape().div;
  assert.deepEqual(sh[0], { h2: ["Titulo"] });
  assert.deepEqual(sh[1].p, ["Hola ", { strong: ["mundo"] }, " y ", { em: ["algo"] }, " con ", { code: ["x()"] }]);
  assert.deepEqual(sh[2], { ul: [{ li: ["uno"] }, { li: ["dos"] }] });
  assert.equal(Object.keys(sh[3])[0], "ol");
  assert.deepEqual(sh[4], { pre: [{ code: ["codigo <b>"] }] });
  const last = sh[5].p;
  assert.equal(last[0], "<img src=x onerror=alert(1)> ", "el HTML no quedo como texto");
  assert.equal(last[1].a[0], "link");
  assert.ok(!JSON.stringify(sh).includes('"a":["malo"]'), "un link javascript: no puede ser link");
});

// ---------- conocimiento del proyecto: la pagina esta en "demo" (un doc publico, uno interno) ----------
await scenario("A.ask responde con los documentos publicos y cita", async () => {
  const r = await visitor().ask("¿en que horario abre la tienda? ¿y el margen?");
  assert.match(r.text, /eco/);
  assert.equal(r.sources[0].path, "publico.md");
  assert.ok(!JSON.stringify(r).includes("73"), "un documento interno llego a la pagina");
});

await scenario("A.ask en vivo trae las fuentes y el texto por pedazos", async () => {
  const pieces = [];
  const r = await visitor().ask("horario de la tienda", { onText: (t, s) => pieces.push(s) });
  assert.ok(pieces.length > 1);
  assert.equal(r.sources[0].path, "publico.md");
  assert.equal(r.text, pieces[pieces.length - 1]);
});

await scenario("A.docs.search devuelve pasajes publicos, sin IA", async () => {
  const p = await visitor().docs.search("margen tienda horario");
  assert.ok(p.length >= 1);
  assert.ok(p.every(x => x.path === "publico.md"), JSON.stringify(p));
  assert.ok("text" in p[0] && "title" in p[0]);
});

await scenario("A.ai.chat con knowledge guarda las fuentes de la ultima respuesta", async () => {
  const c = visitor().ai.chat({ system: "Sos el asistente de la tienda", knowledge: true });
  await c.send("¿abren los lunes?");
  assert.equal(c.sources[0].path, "publico.md");
  await c.send("¿y el horario de la tienda los viernes?", () => {});
  assert.equal(c.sources[0].path, "publico.md");
  assert.equal(c.history.length, 4);
});

console.log(results.join("\n"));
process.exit(results.some(r => r.startsWith("FAIL")) ? 1 : 0);
