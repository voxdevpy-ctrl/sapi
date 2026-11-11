"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const sessionStart = (() => {
  const d = new Date(), o = -d.getTimezoneOffset(), p = (n) => String(Math.floor(Math.abs(n))).padStart(2, "0");
  return new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19) + (o >= 0 ? "+" : "-") + p(o / 60) + ":" + p(o % 60);
})();
let view = "assuntos", queue = [], current = null, reason = null, paused = false, undoStack = [], focusId = null;
let session = { local: true, autenticado: true };
// ---------------------------------------------------------------- só um vídeo tocando por vez
// tocar um vídeo pausa os outros; sair da tela, trocar de seção ou esconder a aba também pausa
const Players = {
  io: "IntersectionObserver" in window ? new IntersectionObserver((es) => es.forEach((e) => { if (!e.isIntersecting && !e.target.paused) e.target.pause(); }), { threshold: 0.25 }) : null,
  watched: new WeakSet(),
  pauseAll(except) { document.querySelectorAll("video").forEach((v) => { if (v !== except && !v.paused) v.pause(); }); },
  scan() { document.querySelectorAll("video").forEach((v) => { if (this.io && !this.watched.has(v)) { this.io.observe(v); this.watched.add(v); } }); },
};
document.addEventListener("play", (e) => { if (e.target.tagName === "VIDEO") Players.pauseAll(e.target); }, true);
// aba escondida: pausa os vídeos e congela qualquer animação (engrenagem, esteira)
document.addEventListener("visibilitychange", () => { if (document.hidden) Players.pauseAll(); document.body.classList.toggle("idle", document.hidden); });
new MutationObserver(() => Players.scan()).observe(document.documentElement, { childList: true, subtree: true });

const VIEWS = ["assuntos", "cortes", "pagina", "producao", "prontos", "relatorios", "arquivo", "painel", "celular"];

async function api(path, body) {
  const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json", "X-Cortes": "1" }, body: JSON.stringify(body) } : {});
  const j = await r.json();
  if (r.status === 401) { showLogin(); throw new Error(j.erro); }
  if (!r.ok) throw new Error(j.erro || r.statusText);
  return j;
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.hidden = false; clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), 2800); }
const field = (label, val, pre) => val ? `<div class="f"><b>${label}</b>${pre ? `<pre>${esc(val)}</pre>` : esc(val)}</div>` : "";
const LANG = { pt: "português", en: "inglês", es: "espanhol", fr: "francês", de: "alemão", it: "italiano", ja: "japonês", ko: "coreano",
  zh: "chinês", ru: "russo", ar: "árabe", hi: "hindi", tl: "tagalo", ka: "georgiano", nl: "holandês" };
const langName = (l) => !l ? "" : LANG[l.toLowerCase().slice(0, 2)] || l;
const langPill = (l, src) => !l ? `<span class="pill">idioma a detectar</span>`
  : l.startsWith("pt") ? `<span class="pill spirit">português${src ? " · " + esc(src) : ""}</span>`
  : `<span class="pill ember">${esc(langName(l))} → legenda em português</span>`;
const fmtDate = (s) => s ? new Date(s).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" }) : "";
const fmtWhen = (s) => { if (!s) return ""; const d = new Date(s), t = new Date(), same = d.toDateString() === t.toDateString();
  return (same ? "hoje " : d.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" }) + " ") + d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" }); };
const fmtT = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const mins = (s) => s == null ? "?" : s < 60 ? `${s}s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`;
const mb = (b) => (b / 1048576).toFixed(1) + " MB";
const count = (id, n) => { const el = $(id); if (el) { el.textContent = n || ""; el.classList.toggle("zero", !n); } };

// ---------------------------------------------------------------- navegação
function go(v) {
  view = v; undoStack = []; $("#sheet").hidden = true; Players.pauseAll();
  $$("[data-view]").forEach((b) => b.classList.toggle("on", b.dataset.view === v));
  const review = v === "assuntos" || v === "cortes";
  $("#v-review").hidden = !review;
  VIEWS.filter((x) => x !== "assuntos" && x !== "cortes").forEach((x) => ($("#v-" + x).hidden = x !== v));
  ({ assuntos: load, cortes: load, pagina, producao: statusBar, prontos, relatorios, arquivo, painel, celular })[v]();
  try { localStorage.setItem("cortes_view", v); } catch { /* sem armazenamento */ }
  scrollTo(0, 0);
}

// ---------------------------------------------------------------- revisão (5 botões)
function participants(p) {
  return Object.values(p || {}).map((x) => `${x.name || x.label}${x.name ? "" : " (não identificado)"}${x.role ? " — " + x.role : ""}`).join("; ");
}
function renderOpp(o) {
  const av = o.availability || {}, rec = o.rec_explain || {};
  $("#c-title").textContent = o.topic;
  $("#c-sub").innerHTML = `${esc(o.source_title)} · ${esc(o.channel)} · <a href="${esc(o.source_url)}" target="_blank" rel="noopener">abrir fonte</a>`;
  $("#c-pills").innerHTML = langPill(o.language, o.language_source)
    + (av.ok ? `<span class="pill ok">disponível · ${Math.round((av.duration || 0) / 60)} min · ${av.best_height || "?"}p</span>` : `<span class="pill bad">disponibilidade não confirmada</span>`)
    + (rec.estrategia ? `<span class="pill">${esc(rec.estrategia)}</span>` : "")
    + (o.confidence != null ? `<span class="pill">confiança ${Math.round(o.confidence * 100)}%</span>` : "");
  $("#c-alerts").innerHTML = (av.problems || []).map((p) => `<div class="alert">${esc(p)}</div>`).join("");
  $("#c-body").innerHTML = [field("Resumo", o.summary), field("Por que foi recomendado", o.reason), field("Estratégia do recomendador", rec.mensagem),
    field("Evidências de interesse", (o.evidence || []).join("\n"), true), field("Potencial editorial", o.potential), field("Nicho", o.niche),
    field("Base de autorização", av.authorization), field("Observações de uso", o.usage_notes), field("Publicado em", o.published_at)].join("");
  $("#player-wrap").hidden = true;
}
function renderCut(c) {
  $("#c-title").textContent = c.title_tmp;
  $("#c-sub").textContent = `${c.video_title} · ${fmtT(c.start)}–${fmtT(c.end)} no original`;
  $("#c-pills").innerHTML = `<span class="pill spirit">${c.duration}s</span><span class="pill">nota ${c.score_total}/10</span>` + langPill(c.language, "áudio");
  $("#c-alerts").innerHTML = (c.alerts || []).map((a) => `<div class="alert">${esc(a)}</div>`).join("");
  const notas = Object.entries(c.scores.notas || {}).map(([k, v]) => `${k} ${v}`).join(" · ");
  $("#c-body").innerHTML = [field("Resumo", c.summary), field("Participantes", participants(c.participants)), field("Por que foi escolhido", c.reason),
    field("Contexto anterior", c.context_before), field("Contexto posterior", c.context_after), field("Notas por critério", notas),
    field("Transcrição do trecho", c.transcript, true)].join("");
  $("#player").src = c.preview_url; $("#player-wrap").hidden = false;
}
async function refreshCounts() {
  const [o, c, r] = await Promise.all([api("/api/queue/opportunities"), api("/api/queue/cuts"), api("/api/prontos")]);
  const ready = r.filter((x) => !String(x.pub_state || "").startsWith("POSTADO") && x.pub_state !== "PUBLICADO").length;
  [["assuntos", o.length], ["cortes", c.length], ["prontos", ready]].forEach(([k, n]) => { count("#n-" + k, n); count("#m-" + k, n); });
  return { o, c };
}
async function load() {
  const { o, c } = await refreshCounts();
  queue = view === "assuntos" ? o : c;
  $("#pedir").hidden = view !== "assuntos";
  $("#review-title").textContent = view === "assuntos" ? "Assuntos para avaliar" : "Cortes para avaliar";
  if (focusId) { const i = queue.findIndex((x) => x.id === focusId); if (i > 0) queue.unshift(...queue.splice(i, 1)); focusId = null; }
  show();
  const s = (await api("/api/stats?since=" + encodeURIComponent(sessionStart)))[view === "assuntos" ? "opportunity" : "cut"];
  $("#session").textContent = `${queue.length} na fila · nesta sessão: ${s.aprovados} aprovados, ${s.adiados} adiados, ${s.descartados} descartados`;
}
function show() {
  current = queue[0] || null;
  $("#paused").hidden = !paused; $("#empty").hidden = paused || !!current;
  $("#card").hidden = paused || !current; $("#controls").hidden = paused || !current;
  if (paused || !current) { $("#player").pause?.(); return; }
  reason = null; $$(".chip").forEach((c) => c.classList.remove("on"));
  view === "assuntos" ? renderOpp(current) : renderCut(current);
}
async function decide(score) {
  if (!current) return;
  try {
    const r = await api("/api/decide", { kind: view === "assuntos" ? "opportunity" : "cut", id: current.id, score, reason });
    undoStack.push({ id: r.decision_id, title: current.topic || current.title_tmp }); toast(`${r.label} — salvo`); $("#player").pause?.(); await load();
  } catch (e) { toast(e.message); }
}
async function undo() {
  const last = undoStack.pop(); if (!last) return toast("Nada para desfazer nesta sessão");
  try { const r = await api("/api/undo", { decision_id: last.id }); focusId = r.item_id; toast(`Desfeito: ${last.title}`); await load(); } catch (e) { toast(e.message); }
}
function bindKeys() {
  document.addEventListener("keydown", (e) => {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName) || e.ctrlKey || e.altKey || e.metaKey || $("#v-review").hidden) return;
    const map = { 1: 100, 2: 50, 3: 0, 4: -50, 5: -100 };
    if (map[e.key] !== undefined && !paused) { e.preventDefault(); decide(map[e.key]); }
    else if (e.key.toLowerCase() === "u") undo();
    else if (e.key.toLowerCase() === "p") { paused = !paused; show(); }
  });
}

// ---------------------------------------------------------------- produção
const ICON = { feito: "✓", agora: "●", aguardando: "○", "na fila": "◌", erro: "✕", repetindo: "↻", interrompido: "⏸" };
const steps = (list) => `<div class="steps">${list.map((s) => `<span class="step ${s.estado.replace(" ", "-")}" title="${esc(s.erro || s.estado)}">${ICON[s.estado] || ""} ${esc(s.nome)}</span>`).join("")}</div>`;
async function statusBar() {
  try {
    const p = await api("/api/producao");
    $("#status").className = "status st-" + p.estado;
    const a = p.agora[0];
    $("#status-text").textContent = a ? `${a.auditando ? "Supervisor auditando · " : ""}${p.resumo} · há ${mins(a.decorrido_s)}${a.estimativa_s ? ` de ~${mins(a.estimativa_s)}` : ""}` : p.resumo;
    if (view === "producao") renderProducao(p);
  } catch { /* login cuida */ }
}
function renderProducao(p) {
  const now = p.agora.map((a) => {
    const pct = a.estimativa_s ? Math.min(95, Math.round(100 * a.decorrido_s / a.estimativa_s)) : null;
    // a esteira (.belt) só anda em cartão de tarefa em andamento: parada = nada processando
    return `<div class="now run"><div class="rep-head"><b class="rep-stage">${esc(a.nome)}</b>${a.auditando ? "<span class='pill spirit'>supervisor auditando</span>" : ""}</div>
      <div class="rep-item">${esc(a.item)}</div>${pct != null ? `<div class="bar"><i style="width:${pct}%"></i></div>` : ""}
      <div class="faint">há ${mins(a.decorrido_s)}${a.estimativa_s ? ` · estimativa ~${mins(a.estimativa_s)}` : ""} · ${esc(a.agente)} → ${esc(a.supervisor)}</div><i class="belt" aria-hidden="true"></i></div>`;
  }).join("") || `<div class="now"><b>${esc(p.resumo)}</b><i class="belt" aria-hidden="true"></i></div>`;
  const fila = p.fila.length ? `<ol class="feed">${p.fila.map((f) => `<li><b>${esc(f.nome)}</b> — ${esc(f.item)}${f.espera_ate ? ` <span class="faint">(tenta de novo ${esc(fmtWhen(f.espera_ate))})</span>` : ""}${f.tentativa > 1 ? ` <span class="pill ember">tentativa ${f.tentativa}</span>` : ""}</li>`).join("")}</ol>` : `<p class="muted">Fila vazia.</p>`;
  const vids = p.videos.map((v) => `<div class="vid"><div class="rep-head"><b>${esc(v.assunto)}</b>${v.concluido ? "<span class='pill ok'>concluído</span>" : ""}</div>
      <div class="pills">${v.duracao_s ? `<span class="pill">${mins(Math.round(v.duracao_s))} de vídeo</span>` : ""}${langPill(v.idioma)}</div>${steps(v.etapas)}
      ${v.cortes.map((c) => `<div class="cut">${esc(c.titulo)} ${c.estado === "AGUARDANDO_APROVACAO" ? "<span class='pill ember'>sua avaliação</span>" : c.estado === "CONCLUIDO" ? "<span class='pill ok'>pronto</span>" : ""}${c.etapas.length ? steps(c.etapas) : ""}</div>`).join("")}</div>`).join("") || `<p class="muted">Nenhum vídeo em produção.</p>`;
  $("#v-producao").innerHTML = `<div class="view-head"><h1>Produção</h1><p class="sub">atualiza a cada 5 s</p></div>${cotaBar(p.armazenamento)}
    <h3>Agora</h3>${now}<h3>Próximas etapas</h3>${fila}<h3>Vídeos</h3>${vids}
    <h3>Atividade recente</h3><ul class="feed">${p.recentes.map((e) => `<li><span class="faint">${esc(fmtWhen(e.ts))}</span> ${esc(e.texto)}</li>`).join("")}</ul>
    <p class="faint">✓ feito · ● agora · ◌ na fila · ○ aguardando · ↻ repetindo · ⏸ interrompido · ✕ bloqueado</p>`;
}

// ---------------------------------------------------------------- prontos (lista compacta: todos visíveis de uma vez)
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); } catch { const t = document.createElement("textarea"); t.value = text; document.body.appendChild(t); t.select(); document.execCommand("copy"); t.remove(); }
  toast("Copiado");
}
const PLAT = { tiktok: "TikTok", instagram: "Instagram" };
async function prontos() {
  const rows = await api("/api/prontos"), sec = $("#v-prontos");
  const posted = (r) => Object.keys(r.marcacoes || {}).length > 0 || r.pub_state === "PUBLICADO";
  const pend = rows.filter((r) => !posted(r)).length;
  const fate = (r) => r.video_excluido_em ? `<p class="fate gone">Vídeo apagado do PC em ${esc(fmtDate(r.video_excluido_em))}${r.nuvem ? " · cópia na nuvem: " + esc(r.nuvem) : ""}. Título, descrição e legendas continuam aqui.</p>`
    : r.exclusao_prevista ? `<p class="fate">O vídeo será apagado do PC a partir de ${esc(new Date(r.exclusao_prevista + "T00:00").toLocaleDateString("pt-BR"))}. Título, descrição e legendas ficam.</p>`
    : posted(r) ? `<p class="fate">O vídeo é apagado no dia seguinte depois de marcado nas duas redes.</p>` : "";
  const platBtns = (r, i) => Object.entries(PLAT).map(([k, n]) => { const m = (r.marcacoes || {})[k];
    return `<button class="btn ${m ? "on-plat" : "sec"}" data-plat="${k}" data-i="${i}" title="${m ? "Postado em " + esc(fmtDate(m.posted_at)) + " · toque para desmarcar" : "Marcar como postado no " + n}">${m ? "✓ " : ""}Postado ${n}</button>`; }).join("");
  const pills = (r) => Object.entries(PLAT).map(([k, n]) => (r.marcacoes || {})[k] ? `<span class="pill ok">${n} ✓</span>` : "").join("")
    || "<span class='pill ember'>para postar</span>";
  const first = rows.findIndex((x) => !posted(x));
  sec.innerHTML = `<div class="view-head"><h1>Prontos para postar</h1><p class="sub">${pend} para postar · ${rows.length - pend} já postados</p></div>`
    + (rows.length ? `<div class="ready-list">${rows.map((r, i) => `<details class="ready" ${i === first ? "open" : ""}>
      <summary><span class="when">${esc(fmtWhen(r.scheduled_for))}</span><span class="t">${esc(r.title)}</span>
        ${r.baixado_em ? `<span class="pill spirit" title="Baixado em ${esc(fmtDate(r.baixado_em))}${r.vezes_baixado > 1 ? ` · ${r.vezes_baixado} vezes` : ""}">Vídeo Baixado</span>` : ""}${pills(r)}${langPill(r.language)}</summary>
      <div class="ready-body">${r.video_excluido_em ? `<div class="gone-box">vídeo apagado</div>` : `<video src="${esc(r.video_url)}" controls preload="none" playsinline></video>`}
        <div><pre>${esc(r.descricao_completa)}</pre><div class="acts">
          ${r.video_excluido_em ? "" : `<a class="btn ${r.baixado_em ? "sec" : ""}" data-dl="${i}" href="${esc(r.video_url)}?baixar=1" download="corte-${i + 1}.mp4">${r.baixado_em ? "Baixar de novo" : "Baixar vídeo"}</a>`}
          <button class="btn sec" data-copy="t${i}">Copiar título</button><button class="btn sec" data-copy="d${i}">Copiar descrição</button></div>
          <div class="acts plats">${platBtns(r, i)}</div>${fate(r)}</div></div></details>`).join("")}</div>`
      : `<div class="empty">Nenhum corte finalizado ainda.</div>`);
  $$("[data-copy]", sec).forEach((b) => (b.onclick = () => { const r = rows[+b.dataset.copy.slice(1)]; copyText(b.dataset.copy[0] === "t" ? r.title : r.descricao_completa); }));
  $$("[data-dl]", sec).forEach((a) => a.addEventListener("click", () => setTimeout(prontos, 2500)));
  $$("[data-plat]", sec).forEach((b) => (b.onclick = async () => {
    const r = rows[+b.dataset.i], k = b.dataset.plat, marked = (r.marcacoes || {})[k];
    if (marked && r.video_excluido_em) return toast("O vídeo já foi apagado; a marcação fica registrada");
    let url = null;
    if (marked) { if (!confirm(`Desmarcar "postado no ${PLAT[k]}"?`)) return; }
    else { url = prompt(`Link do post no ${PLAT[k]} (opcional):`); if (url === null) return; }
    try { await api("/api/postado", { export_id: r.id, plataforma: k, url, desfazer: !!marked }); toast(marked ? "Desmarcado" : `Postado no ${PLAT[k]} ✓`); prontos(); } catch (e) { toast(e.message); }
  }));
}

// ---------------------------------------------------------------- relatórios dos supervisores
let repFilter = "problemas", repStage = "";
const STATUS_TXT = { APROVADO: "Etapa aprovada", APROVADO_COM_RESSALVAS: "Etapa com ressalvas", REPROVADO: "Etapa reprovada" };
const VOTO_CLS = { 100: "ok", 50: "ok", 0: "", "-50": "bad", "-100": "bad" };
const voto = (v) => !v ? "" : `<span class="pill ${VOTO_CLS[v.score]}">Seu voto: ${esc(v.label.toLowerCase())}${v.score < 0 ? " · não será produzido" : v.score === 0 ? " · adiado" : ""}</span>`;
const DEC_TXT = { AVANCAR: "gerente: avançou", REPETIR_OU_BLOQUEAR: "gerente: repetir/bloquear" };
const lst = (cls, title, items) => items && items.length ? `<div class="blk ${cls}"><h4>${title}</h4><ul>${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : "";
async function relatorios() {
  const rows = await api("/api/relatorios"), sec = $("#v-relatorios");
  const n = (f) => rows.filter(f).length;
  const filters = { problemas: (r) => r.status !== "APROVADO", REPROVADO: (r) => r.status === "REPROVADO",
    APROVADO_COM_RESSALVAS: (r) => r.status === "APROVADO_COM_RESSALVAS", APROVADO: (r) => r.status === "APROVADO", todos: () => true };
  const stages = [...new Set(rows.map((r) => r.etapa_nome))];
  const shown = rows.filter(filters[repFilter]).filter((r) => !repStage || r.etapa_nome === repStage);
  sec.innerHTML = `<div class="view-head"><h1>Relatórios dos supervisores</h1><p class="sub">${rows.length} relatórios · "Etapa aprovada" quer dizer que o supervisor conferiu o trabalho automático daquela etapa (ex.: o vídeo está disponível). Quem decide se um assunto ou corte é produzido é o seu voto.</p></div>
    <div class="toolbar"><div class="seg">${[["problemas", "Com problema"], ["REPROVADO", "Reprovados"], ["APROVADO_COM_RESSALVAS", "Ressalvas"], ["APROVADO", "Aprovados"], ["todos", "Todos"]]
      .map(([k, l]) => `<button data-f="${k}" class="${repFilter === k ? "on" : ""}">${l}<span class="n">${n(filters[k])}</span></button>`).join("")}</div>
      <select id="rep-stage"><option value="">Todas as etapas</option>${stages.map((s) => `<option ${s === repStage ? "selected" : ""}>${esc(s)}</option>`).join("")}</select></div>
    ${shown.map((r) => { const b = r.body || {};
      return `<article class="rep ${r.status}"><div class="rep-head"><span class="badge ${r.status}">${STATUS_TXT[r.status] || r.status}</span>
        <span class="rep-stage">${esc(r.etapa_nome)}</span>${r.manager_decision ? `<span class="decision ${r.manager_decision}">${DEC_TXT[r.manager_decision] || r.manager_decision}</span>` : ""}
        <span class="rep-when">${esc(fmtWhen(r.ts))}</span></div>
        ${r.item ? `<div class="rep-item">${esc(r.item)} ${voto(r.seu_voto)}</div>` : ""}
        <div class="rep-trail"><b>${esc(r.agent)}</b> → supervisor <b>${esc(r.supervisor)}</b> · ${esc(b.fez || "")}</div>
        ${lst("prob", "Problemas", b.problemas)}${lst("ress", "Ressalvas", b.ressalvas)}${lst("rec", "Recomendação", [...new Set(b.correcoes || [])])}
        ${(b.evidencias || []).filter(Boolean).length ? `<details class="evid"><summary>Evidências (${b.evidencias.filter(Boolean).length})</summary><ul>${b.evidencias.filter(Boolean).map((e) => `<li>${esc(e)}</li>`).join("")}</ul></details>` : ""}
      </article>`; }).join("") || `<div class="empty">Nenhum relatório neste filtro.</div>`}`;
  $$("[data-f]", sec).forEach((b) => (b.onclick = () => { repFilter = b.dataset.f; relatorios(); }));
  $("#rep-stage").onchange = (e) => { repStage = e.target.value; relatorios(); };
}

// ---------------------------------------------------------------- adiados e rejeitados
let arqTab = "ADIADO";
async function arquivo() {
  const d = await api("/api/arquivo"), sec = $("#v-arquivo");
  const tabs = [["ADIADO", "Adiados", d.ADIADO.assuntos.length + d.ADIADO.cortes.length], ["REJEITADO", "Rejeitados por você", d.REJEITADO.assuntos.length + d.REJEITADO.cortes.length],
    ["DESCARTADO", "Descartados pelo supervisor", d.DESCARTADO.length]];
  const row = (title, sub, why, btn) => `<div class="row"><div class="grow"><div class="t">${esc(title)}</div><div class="s">${sub}</div>${why ? `<div class="why">${esc(why)}</div>` : ""}</div>${btn || ""}</div>`;
  const decLine = (x) => [x.label, x.decided_at ? fmtWhen(x.decided_at) : ""].filter(Boolean).map(esc).join(" · ");
  let body;
  if (arqTab === "DESCARTADO") body = `<p class="sub">Cortes que a revisão editorial independente reprovou antes de chegar a você.</p><div class="list">${d.DESCARTADO.map((c) => row(c.title_tmp, esc(c.video || ""), c.motivo)).join("") || "<div class='empty'>Nada aqui.</div>"}</div>`;
  else {
    const g = d[arqTab], btn = (k, id) => arqTab === "ADIADO" ? `<button class="btn sec" data-rec="${k}:${id}">Reavaliar</button>` : "";
    body = `<h3>Assuntos</h3><div class="list">${g.assuntos.map((a) => row(a.topic, `${esc(a.channel || "")} · ${decLine(a)}`, a.reason && a.reason !== "Sem motivo informado" ? a.reason : "", btn("opportunity", a.id))).join("") || "<p class='muted'>Nenhum.</p>"}</div>
      <h3>Cortes</h3><div class="list">${g.cortes.map((c) => row(c.title_tmp, `${esc(c.video || "")} · ${decLine(c)}`, c.reason && c.reason !== "Sem motivo informado" ? c.reason : "", btn("cut", c.id))).join("") || "<p class='muted'>Nenhum.</p>"}</div>`;
  }
  sec.innerHTML = `<div class="view-head"><h1>Adiados e rejeitados</h1></div>
    <div class="toolbar"><div class="seg">${tabs.map(([k, l, n]) => `<button data-a="${k}" class="${arqTab === k ? "on" : ""}">${l}<span class="n">${n}</span></button>`).join("")}</div></div>${body}`;
  $$("[data-a]", sec).forEach((b) => (b.onclick = () => { arqTab = b.dataset.a; arquivo(); }));
  $$("[data-rec]", sec).forEach((b) => (b.onclick = async () => {
    const [kind, id] = b.dataset.rec.split(":");
    try { await api("/api/reconsider", { kind, id }); toast("Voltou para a fila"); arquivo(); refreshCounts(); } catch (e) { toast(e.message); }
  }));
}

// ---------------------------------------------------------------- painel
const cotaBar = (c) => !c ? "" : `<div class="cota ${c.pausado ? "cheia" : c.pct >= 85 ? "quase" : ""}">
  <div class="rep-head"><b>${(c.usado / 2 ** 30).toFixed(1)} GB de ${(c.limite / 2 ** 30).toFixed(0)} GB</b>
  <span class="faint">${c.pausado ? "limite atingido: a produção está pausada até liberar espaço" : `${((c.limite - c.usado) / 2 ** 30).toFixed(1)} GB livres na cota`}</span></div>
  <div class="bar"><i style="width:${Math.min(100, c.pct)}%"></i></div></div>`;
const tbl = (rows, cols) => rows && rows.length ? `<table class="tbl"><tr>${cols.map((c) => `<th>${c[0]}</th>`).join("")}</tr>${rows.map((r) => `<tr>${cols.map((c) => `<td>${c[2] ? c[2](r) : esc(r[c[1]])}</td>`).join("")}</tr>`).join("")}</table>` : `<p class="muted">Nenhum registro.</p>`;
const w = (x) => `<span class="weight ${x.peso > 0 ? "pos" : x.peso < 0 ? "neg" : ""}">${x.peso > 0 ? "+" : ""}${x.peso.toFixed(2)}</span>`;
async function claudeCode(refresh) {
  const st = $("#cc-st"); if (!st) return;
  st.textContent = "Verificando…";
  try {
    const c = await api("/api/claude-code" + (refresh ? "?verificar=1" : ""));
    const ok = c.instalado && c.login_salvo;
    st.innerHTML = `<p><span class="pill ${ok ? "ok" : "ember"}">${ok ? "Conectado" : c.instalado ? "Falta o login" : "Não instalado"}</span>
      ${c.versao ? ` versão ${esc(c.versao)}` : ""}${c.habilitado ? "" : " · agentes desligados na configuração"}</p>
      ${c.orientacao ? `<p class="faint">${esc(c.orientacao)} <a href="${esc(c.site)}" target="_blank" rel="noopener">Site oficial do Claude Code</a></p>` : ""}`;
  } catch (e) { st.textContent = e.message; }
}
async function painel() {
  const d = await api("/api/dashboard"), ind = d.indicadores, arm = d.armazenamento;
  const kpi = (l, v, c = "") => `<div class="kpi ${c}"><b>${esc(v)}</b><span>${esc(l)}</span></div>`;
  $("#v-painel").innerHTML = `<div class="view-head"><h1>Painel</h1></div>
    <h3>Claude Code</h3><div class="cc" id="cc-box"><p class="sub">Cada pessoa conecta o Claude Code instalado e logado no PRÓPRIO computador. Nenhuma senha ou chave passa pelo SAPI.</p>
      <button class="btn" id="cc-btn" type="button">Conectar Claude Code</button><div id="cc-st" aria-live="polite"></div></div>
    <h3>Produção</h3><div class="kpis">${kpi("assuntos para avaliar", ind.oportunidades.AGUARDANDO_APROVACAO || 0, "ember")}${kpi("cortes para avaliar", ind.cortes.AGUARDANDO_APROVACAO || 0, "ember")}
      ${kpi("cortes em produção", ind.cortes.APROVADO || 0, "spirit")}${kpi("cortes prontos", ind.cortes.CONCLUIDO || 0)}${kpi("exportados", ind.exportados)}
      ${kpi("postados", (ind.publicacoes.POSTADO_PELO_USUARIO || 0) + (ind.publicacoes.PUBLICADO || 0))}</div>
    <h3>Armazenamento</h3>${cotaBar(d.cota)}<div class="kpis">${kpi("originais temporários", mb(arm.ocupado_originais))}${kpi("economizado na compressão", mb(arm.economia_compressao))}${kpi("disco livre", (arm.disco_livre_mb / 1024).toFixed(1) + " GB")}</div>
    <h3>Próximas exclusões (14 dias)</h3>${tbl(arm.proximas_exclusoes, [["Vídeo", "titulo"], ["Exclusão prevista", "", (r) => esc(fmtWhen(r.quando))], ["Estado", "estado"]])}
    <h3>Agentes e supervisores</h3>${tbl(d.agentes, [["Agente", "agente"], ["Supervisor", "supervisor"], ["Etapas", "", (r) => Object.entries(r.etapas).map(([s, v]) => `<b>${esc(s)}</b> ${Object.entries(v).map(([k, n]) => `${k.toLowerCase()} ${n}`).join(" · ") || "—"}`).join("<br>")]])}
    <h3>Perfil editorial aprendido</h3><p class="sub">De −1 a +1; avaliações recentes pesam mais e uma isolada pesa pouco. Orienta, não proíbe.</p>
    ${["assuntos", "cortes"].map((k) => tbl([...d.perfil[k].positivas, ...d.perfil[k].negativas.filter((x) => x.peso < 0)], [["Característica (" + k + ")", "caracteristica"], ["Peso", "", w], ["Avaliações", "avaliacoes"]])).join("<br>")}
    <h3>Histórico de decisões</h3>${tbl(d.historico.slice(0, 40), [["Quando", "", (r) => esc(fmtWhen(r.decided_at))], ["Tipo", "", (r) => r.kind === "cut" ? "corte" : "assunto"], ["Decisão", "label"], ["Motivo", "reason"], ["Item", "", (r) => esc((r.snapshot || {}).topic || r.item_id)]])}
    <h3>Limpezas realizadas</h3>${tbl(arm.limpezas.slice(0, 20), [["Quando", "", (r) => esc(fmtWhen(r.ts))], ["Ação", "action"], ["Liberado", "", (r) => mb(r.bytes || 0)], ["Detalhe", "detail"]])}
    <h3>Configurações</h3><details><summary class="muted">Editar configurações avançadas (JSON)</summary>
      <textarea id="cfg">${esc(JSON.stringify(d.config, null, 2))}</textarea><p><button class="btn" id="save-cfg">Salvar configurações</button></p></details>`;
  $("#save-cfg").onclick = async () => { try { await api("/api/config", { config: JSON.parse($("#cfg").value) }); toast("Configurações salvas"); } catch (e) { toast(e.message); } };
  $("#cc-btn").onclick = () => claudeCode(true);
  claudeCode(false);
}

// ---------------------------------------------------------------- celular
async function celular() {
  const sec = $("#v-celular");
  if (!session.local) { sec.innerHTML = `<div class="view-head"><h1>Celular</h1></div><div class="empty">Você já está conectado pelo celular.</div>`; return; }
  const r = await api("/api/remoto"), links = [...r.tailscale, ...r.rede_local];
  $("#remote-dot").classList.toggle("on", r.habilitado && r.escutando_na_rede);
  sec.innerHTML = `<div class="view-head"><h1>Acesso pelo celular</h1></div><div class="remote-card">
    <div class="remote-state"><span class="pill ${r.habilitado ? "ok" : ""}">acesso ${r.habilitado ? "ligado" : "desligado"}</span>
      <span class="pill ${r.pin_definido ? "ok" : "bad"}">PIN ${r.pin_definido ? "definido" : "não definido"}</span>
      <span class="pill ${r.tailscale_detectado ? "ok" : "bad"}">Tailscale ${r.tailscale_detectado ? "conectado" : "não detectado"}</span>
      ${r.habilitado && !r.escutando_na_rede ? `<span class="pill ember">reabra o .bat para valer</span>` : ""}</div>
    ${links.length && r.escutando_na_rede ? `<div><p class="sub">No celular, abra:</p>${links.map((l) => `<div class="url"><span>${esc(l)}</span><button class="btn sec" data-url="${esc(l)}">Copiar</button></div>`).join("")}</div>` : ""}
    <div class="toolbar"><input id="new-pin" type="password" inputmode="numeric" placeholder="novo PIN (6–12 dígitos)" maxlength="12">
      <button class="btn sec" id="set-pin">Definir PIN</button><button class="btn" id="toggle-remote">${r.habilitado ? "Desligar acesso" : "Ligar acesso"}</button>
      <label class="muted"><input id="lan" type="checkbox" ${r.permitir_rede_local ? "checked" : ""}> também pela rede Wi-Fi de casa</label></div>
    <ol class="steps-guide"><li>App Tailscale do celular ligado (Connected).</li><li>Abra o endereço acima e digite o PIN.</li>
      <li>No Chrome: menu ⋮ → “Adicionar à tela inicial”.</li><li>Funciona só com o .bat aberto neste PC.</li></ol></div>`;
  $$("[data-url]", sec).forEach((b) => (b.onclick = () => copyText(b.dataset.url)));
  $("#set-pin").onclick = async () => { try { await api("/api/pin", { pin: $("#new-pin").value }); toast("PIN definido"); celular(); } catch (e) { toast(e.message); } };
  const save = async (patch) => { try { const d = await api("/api/dashboard"); Object.assign(d.config.acesso_remoto, patch); await api("/api/config", { config: d.config }); toast("Salvo — reabra o .bat"); celular(); } catch (e) { toast(e.message); } };
  $("#toggle-remote").onclick = () => (!r.pin_definido && !r.habilitado ? toast("Defina um PIN antes") : save({ habilitado: !r.habilitado }));
  $("#lan").onchange = (e) => save({ permitir_rede_local: e.target.checked });
}

async function reportBadge() {
  try { const rows = await api("/api/relatorios"); const since = Date.now() - 864e5; count("#n-relatorios", rows.filter((r) => r.status === "REPROVADO" && new Date(r.ts) > since).length); } catch { /* opcional */ }
}

// ---------------------------------------------------------------- início
function showLogin() { $("#login").hidden = false; $("#pin").focus(); }
async function init() {
  session = await api("/api/sessao");
  if (session.prototipo) $("#proto-banner").hidden = false;
  if (!session.autenticado) {
    showLogin();
    $("#login-form").onsubmit = async (e) => { e.preventDefault(); try { await api("/api/login", { pin: $("#pin").value }); location.reload(); } catch (err) { toast(err.message); $("#pin").value = ""; } };
    return;
  }
  const meta = await api("/api/meta");
  $("#reasons").innerHTML = meta.motivos.filter((m) => m !== "Sem motivo informado").map((m) => `<button class="chip">${esc(m)}</button>`).join("");
  $$(".chip").forEach((c) => (c.onclick = () => { const on = !c.classList.contains("on"); $$(".chip").forEach((x) => x.classList.remove("on")); c.classList.toggle("on", on); reason = on ? c.textContent : null; }));
  $$(".b").forEach((b) => (b.onclick = () => decide(+b.dataset.score)));
  $$("[data-view]").forEach((b) => (b.onclick = () => go(b.dataset.view)));
  $("#more").onclick = () => ($("#sheet").hidden = false);
  $("#sheet").onclick = (e) => { if (e.target.id === "sheet") $("#sheet").hidden = true; };
  $("#go-celular").onclick = () => go("celular");
  $("#prev").onclick = undo; $("#pause").onclick = () => { paused = true; show(); }; $("#resume").onclick = () => { paused = false; show(); };
  bindKeys();
  let start = "assuntos"; try { start = localStorage.getItem("cortes_view") || start; } catch { /* sem armazenamento */ }
  go(VIEWS.includes(start) ? start : "assuntos");
  statusBar(); reportBadge(); setInterval(statusBar, 5000);
  setInterval(() => { (view === "assuntos" || view === "cortes") && !current ? load() : refreshCounts(); }, 20000);
}
init().catch((e) => toast("Erro ao iniciar: " + e.message));

// ---------------------------------------------------------------- pedir assuntos
function initPedir() {
  const f = $("#pedir-form"); if (!f) return;
  f.onsubmit = async (e) => {
    e.preventDefault();
    const d = Object.fromEntries(new FormData(f).entries());
    if (!d.tema && !d.categoria) return toast("Escreva um tema ou escolha uma categoria");
    try { await api("/api/pesquisar", d); toast("Pesquisa na fila: acompanhe em Produção"); $("#pedir").open = false; f.reset(); }
    catch (err) { toast(err.message); }
  };
}
initPedir();

// ---------------------------------------------------------------- vídeos de página
const PG_STATE = { AGUARDANDO_APROVACAO: ["Para aprovar", "ember"], APROVADO: ["Aprovado · na fila", "spirit"],
  BAIXADO: ["Baixado · montando", "spirit"], MONTADO: ["Montado · conferindo", "spirit"],
  AGUARDANDO_APROVACAO_FINAL: ["Aprovar resultado", "ember"], PRONTO: ["Pronto p/ postar", "ok"], DESCARTADO: ["Descartado", ""],
  RESERVA: ["Reserva (não precisou)", ""], BLOQUEADO: ["Travado · ver motivo", ""] };
// categorias separadas: fonte aprovada (em andamento), cortes já criados e descartados — nunca misturados
const PG_CATS = [["criados", "Cortes criados"], ["aprovados", "Aprovados"], ["descartados", "Descartados"]];
let pgCat = "criados";
function pgCatOf(r) {
  if (["DESCARTADO", "BLOQUEADO", "RESERVA"].includes(r.state)) return "descartados";
  if (["PRONTO", "AGUARDANDO_APROVACAO_FINAL"].includes(r.state) && r.final_path) return "criados";
  return "aprovados";
}
async function pagina() {
  const d = await api("/api/pagina"), sec = $("#v-pagina"), rows = d.itens;
  const vote = (r) => `<div class="acts">${[[100, "+100"], [50, "+50"], [0, "0"], [-50, "−50"], [-100, "−100"]].map(([v, l]) =>
    `<button class="btn ${v > 0 ? "" : "sec"}" data-pg="${esc(r.id)}" data-score="${v}">${l}</button>`).join("")}</div>`;
  const card = (r) => {
    const [txt, cls] = PG_STATE[r.state] || [r.state, ""], m = r.meta || {}, t = r.tarefa;
    const falha = t && ["FALHA", "BLOQUEADO"].includes(t.state) ? `<p class="fate gone">${esc(t.stage)}: ${esc(t.error || t.state)}</p>` : "";
    const media = r.video_url && !r.video_excluido_em ? `<video src="${esc(r.video_url)}" poster="/media/pagina-capa/${esc(r.id)}" controls preload="none" playsinline></video>` : "";
    const dl = r.export_id && !r.video_excluido_em ? `<a class="btn ${r.baixado_em ? "sec" : ""}" data-pgdl href="${esc(r.video_url)}?baixar=1" download="pagina-${esc(r.id)}.mp4">${r.baixado_em ? "Baixar de novo" : "Baixar vídeo"}</a>` : "";
    const plats = r.export_id ? `<div class="acts plats">${Object.entries(PLAT).map(([k, n]) => { const mk = (r.marcacoes || {})[k];
      return `<button class="btn ${mk ? "on-plat" : "sec"}" data-pgplat="${k}" data-e="${esc(r.export_id)}" data-marcado="${mk ? 1 : ""}" aria-pressed="${mk ? "true" : "false"}" title="${mk ? "Postado em " + esc(fmtDate(mk.posted_at)) + " · toque para desmarcar" : "Marcar como postado no " + n}">${mk ? "✓ " : ""}${mk ? "Postado" : "Pendente"} ${n}</button>`; }).join("")}</div>` : "";
    return `<details class="ready" ${["AGUARDANDO_APROVACAO", "AGUARDANDO_APROVACAO_FINAL", "PRONTO"].includes(r.state) ? "open" : ""}>
      <summary><span class="t">${esc(r.caption || (r.jokes || [])[0] || r.title || r.id)}</span>
        <span class="pill ${cls}">${esc(txt)}</span>${r.baixado_em ? `<span class="pill spirit">Vídeo Baixado</span>` : ""}${r.auto ? `<span class="pill" title="${esc(r.approval1 || "")}">teste · aprovação automática</span>` : ""}</summary>
      <div class="ready-body">${media}<div>
        <p class="sub">Fonte: <a href="${esc(r.source_url)}" target="_blank" rel="noopener">${esc(r.title || r.source_url)}</a> · ${esc(r.channel || "")}
          · ${(r.views || 0).toLocaleString("pt-BR")} visualizações${m.curtidas != null ? ` · ${(+m.curtidas).toLocaleString("pt-BR")} curtidas` : ""}${m.comentarios != null ? ` · ${(+m.comentarios).toLocaleString("pt-BR")} comentários` : ""} · ${Math.round(r.duration || 0)} s${m.engajamento_por_mil != null ? ` · engajamento ${esc(m.engajamento_por_mil)}/mil` : ""}${m.origem ? ` · ${m.origem === "br" ? "brasileiro" : m.origem === "gringo" ? "gringo" : "origem incerta"}` : ""}</p>
        ${m.o_que_acontece ? `<p><b>No vídeo:</b> ${esc(m.o_que_acontece)}</p>` : ""}
        ${r.caption ? "" : `<p><b>Piadas sugeridas:</b><br>${(r.jokes || []).map(esc).join("<br>")}</p>`}
        ${m.motivo ? `<p class="faint">Por que foi escolhido: ${esc(m.motivo)} (humor ${esc(m.nota_humor)}/10)</p>` : ""}
        ${m.parece_ia || m.montador_ia || m.repost === "muito" || m.montador_repost === "muito" ? `<p class="faint">Atenção: ${m.parece_ia || m.montador_ia ? "parece feito por IA" : "muito repostado"}, entrou só por ser muito viral. ${esc(m.montador_sinais || m.sinais_ia_repost || "")}</p>` : ""}
        ${r.description ? `<pre>${esc(r.description)}</pre>` : ""}${m.recusa ? `<p class="fate gone">Recusado pelo montador: ${esc(m.recusa)}</p>` : ""}${m.revisao ? `<p class="fate gone">Revisão: ${esc(m.revisao)}</p>` : ""}${falha}
        ${["AGUARDANDO_APROVACAO", "AGUARDANDO_APROVACAO_FINAL"].includes(r.state) ? vote(r) : ""}
        <div class="acts">${dl}${r.description ? `<button class="btn sec" data-pgcopy="${esc(r.id)}">Copiar descrição</button>` : ""}</div>${plats}</div></div></details>`;
  };
  sec.innerHTML = `<div class="view-head"><h1>Vídeos de Página</h1><p class="sub">Vídeos curtos no formato de post da sua página · use só vídeos seus ou com permissão do autor${d.foco === "pagina" ? " · <b>foco total: as outras produções estão pausadas até este lote acabar</b>" : ""}</p></div>
    <form class="pedir-form pg-pedir" id="pg-form"><div class="row2"><label>Quantos vídeos<input name="quantidade" type="number" min="1" max="20" value="5"></label>
    <label>&nbsp;<button class="btn" type="submit">Buscar vídeos virais</button></label></div></form>
    <div class="seg" role="tablist" aria-label="Categorias">${PG_CATS.map(([k, n]) => `<button class="seg-b ${pgCat === k ? "on" : ""}" role="tab" aria-selected="${pgCat === k}" data-pgcat="${k}">${n} <b class="count">${rows.filter((r) => pgCatOf(r) === k).length}</b></button>`).join("")}</div>
    ${(() => { const vis = rows.filter((r) => pgCatOf(r) === pgCat); return vis.length ? `<div class="ready-list">${vis.map(card).join("")}</div>` : `<div class="empty">Nada em "${esc(PG_CATS.find(([k]) => k === pgCat)[1])}".</div>`; })()}`;
  $$("[data-pgcat]", sec).forEach((b) => (b.onclick = () => { pgCat = b.dataset.pgcat; pagina(); }));
  $("#pg-form").onsubmit = async (e) => { e.preventDefault(); try { await api("/api/pagina/buscar", Object.fromEntries(new FormData(e.target).entries())); toast("Busca na fila: acompanhe em Produção"); pagina(); } catch (err) { toast(err.message); } };
  $$("[data-pg]", sec).forEach((b) => (b.onclick = async () => { try { await api("/api/pagina/decidir", { id: b.dataset.pg, score: +b.dataset.score }); toast("Salvo"); pagina(); } catch (e) { toast(e.message); } }));
  $$("[data-pgdl]", sec).forEach((a) => a.addEventListener("click", () => setTimeout(pagina, 2500)));
  $$("[data-pgcopy]", sec).forEach((b) => (b.onclick = () => copyText(rows.find((r) => r.id === b.dataset.pgcopy).description)));
  $$("[data-pgplat]", sec).forEach((b) => (b.onclick = async () => {
    if (b.disabled) return;  // evita marcação duplicada com toque duplo
    const k = b.dataset.pgplat, marcado = !!b.dataset.marcado;
    let url = null;
    if (marcado) { if (!confirm(`Desmarcar "postado no ${PLAT[k]}"?`)) return; }
    else { url = prompt(`Link do post no ${PLAT[k]} (opcional):`); if (url === null) return; }
    b.disabled = true;
    try { await api("/api/postado", { export_id: b.dataset.e, plataforma: k, url, desfazer: marcado }); toast(marcado ? "Desmarcado" : `Postado no ${PLAT[k]} ✓`); pagina(); }
    catch (e) { b.disabled = false; toast(e.message); } }));
}
