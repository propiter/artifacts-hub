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

function visitor({ key } = {}) {
  const store = new Map();
  const doc = { hidden: false };
  const ctx = {
    fetch, setTimeout, clearTimeout, console, crypto, TextEncoder, Promise, JSON, Math, Date,
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

console.log(results.join("\n"));
process.exit(results.some(r => r.startsWith("FAIL")) ? 1 : 0);
