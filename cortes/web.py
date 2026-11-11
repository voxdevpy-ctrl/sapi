"""Interface local (http://127.0.0.1:8770) — filas de aprovação com cinco botões e painel."""
from __future__ import annotations

import json
import mimetypes
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, cota, db, decisions, llm, log, orchestrator, pagina, pos_postagem, progress, recommend, remote, storage, supervisors

UI = Path(__file__).parent / "ui"


def opp_queue() -> list[dict]:
    rows = db.query("SELECT * FROM opportunities WHERE state='AGUARDANDO_APROVACAO'")
    return sorted(rows, key=lambda r: -float((r.get("rec_explain") or {}).get("valor", 0)))


def cut_queue() -> list[dict]:
    rows = db.query("SELECT c.*, v.title AS video_title, v.url AS video_url, v.channel AS video_channel, "
                    "t.language AS language FROM cuts c JOIN videos v ON v.id=c.video_id "
                    "LEFT JOIN transcripts t ON t.video_id=c.video_id WHERE c.state='AGUARDANDO_APROVACAO' "
                    "ORDER BY c.video_id, c.score_total DESC")
    for r in rows:
        r["duration"] = round(sum(b - a for a, b in r["scores"]["intervalos"]), 1)
        r["preview_url"] = f"/media/preview/{r['id']}"
    return rows


def dashboard() -> dict:
    tasks = db.query("SELECT stage, state, COUNT(*) n FROM tasks GROUP BY stage, state")
    agents = {}
    for stage, (agent, sup, _) in supervisors.REGISTRY.items():
        a = agents.setdefault(agent, {"agente": agent, "supervisor": sup, "etapas": {}})
        a["etapas"][stage] = {t["state"]: t["n"] for t in tasks if t["stage"] == stage}
    reports = db.query("SELECT id, ts, stage, agent, supervisor, status, manager_decision, body FROM reports ORDER BY id DESC LIMIT 40")
    for r in reports:
        r["texto"] = supervisors.render_text(r["body"])
        del r["body"]
    return {"cota": cota.status(), 
        "agentes": list(agents.values()), "relatorios": reports,
        "falhas": db.query("SELECT id, stage, item_id, state, attempts, error, updated_at FROM tasks "
                           "WHERE state IN ('FALHA','BLOQUEADO') OR (error IS NOT NULL AND state='PENDENTE') ORDER BY updated_at DESC LIMIT 50"),
        "indicadores": {
            "oportunidades": {r["state"]: r["n"] for r in db.query("SELECT state, COUNT(*) n FROM opportunities GROUP BY state")},
            "cortes": {r["state"]: r["n"] for r in db.query("SELECT state, COUNT(*) n FROM cuts GROUP BY state")},
            "exportados": db.query("SELECT COUNT(*) n FROM exports")[0]["n"],
            "publicacoes": {r["state"]: r["n"] for r in db.query("SELECT state, COUNT(*) n FROM publications GROUP BY state")}},
        "armazenamento": storage.storage_overview(),
        "perfil": {"assuntos": recommend.profile_summary("opportunity"), "cortes": recommend.profile_summary("cut"),
                   "mudancas": db.query("SELECT * FROM profile_changes ORDER BY id DESC LIMIT 20")},
        "historico": db.query("SELECT id, kind, item_id, score, label, reason, decided_at, undone, snapshot FROM decisions ORDER BY id DESC LIMIT 100"),
        "adiados": {"assuntos": db.query("SELECT id, topic, source_url, channel FROM opportunities WHERE state='ADIADO'"),
                    "cortes": db.query("SELECT id, title_tmp, video_id FROM cuts WHERE state='ADIADO'")},
        "rejeitados": {"assuntos": db.query("SELECT id, topic, channel FROM opportunities WHERE state='REJEITADO' ORDER BY rowid DESC LIMIT 50"),
                       "cortes": db.query("SELECT id, title_tmp FROM cuts WHERE state='REJEITADO' ORDER BY rowid DESC LIMIT 50")},
        "produzidos": db.query("SELECT e.*, p.state AS pub_state, p.url AS pub_url FROM exports e "
                               "LEFT JOIN publications p ON p.export_id=e.id ORDER BY e.scheduled_for DESC LIMIT 50"),
        "config": public_config(),
    }


def report_list() -> list[dict]:
    """Relatórios estruturados para a aba Relatórios (filtráveis por status e etapa)."""
    rows = db.query("SELECT r.id, r.ts, r.stage, r.agent, r.supervisor, r.status, r.manager_decision, r.body, "
                    "t.item_type, t.item_id FROM reports r LEFT JOIN tasks t ON t.id=r.task_id ORDER BY r.id DESC LIMIT 300")
    votes: dict = {}

    def vote(kind, item_id):
        # sua decisão atual (a mais recente não desfeita) — o "Aprovado" do relatório é do supervisor, não seu
        if (kind, item_id) not in votes:
            d = db.query("SELECT score, label FROM decisions WHERE kind=? AND item_id=? AND undone=0 "
                         "ORDER BY id DESC LIMIT 1", (kind, item_id))
            votes[(kind, item_id)] = d[0] if d else None
        return votes[(kind, item_id)]

    for r in rows:
        r["etapa_nome"] = progress.LABEL.get(r["stage"], r["stage"])
        r["item"] = progress._title(r) if r.get("item_id") else ""
        kind, item_id = r.get("item_type"), r.get("item_id")
        if kind == "video":
            v = db.get("videos", item_id)
            kind, item_id = "opportunity", v and v.get("opportunity_id")
        r["seu_voto"] = vote(kind, item_id) if kind in ("opportunity", "cut") and item_id else None
    return rows


def archive() -> dict:
    """Adiados, rejeitados por você e descartados pelo supervisor (com motivo e data da decisão)."""
    def last_dec(kind, item):
        d = db.query("SELECT score, label, reason, decided_at FROM decisions WHERE kind=? AND item_id=? AND undone=0 "
                     "ORDER BY id DESC LIMIT 1", (kind, item))
        return d[0] if d else {}
    out = {}
    for st in ("ADIADO", "REJEITADO"):
        out[st] = {"assuntos": [{**r, **last_dec("opportunity", r["id"])} for r in db.query(
                       "SELECT id, topic, channel, source_url, language FROM opportunities WHERE state=?", (st,))],
                   "cortes": [{**r, **last_dec("cut", r["id"])} for r in db.query(
                       "SELECT c.id, c.title_tmp, c.summary, v.title AS video FROM cuts c LEFT JOIN videos v ON v.id=c.video_id "
                       "WHERE c.state=?", (st,))]}
    out["DESCARTADO"] = [{"id": r["id"], "title_tmp": r["title_tmp"], "video": r["video"],
                          "motivo": next((a for a in reversed(r["alerts"] or []) if "supervisor" in a or "duração" in a), "")}
                         for r in db.query("SELECT c.id, c.title_tmp, c.alerts, v.title AS video FROM cuts c "
                                           "LEFT JOIN videos v ON v.id=c.video_id WHERE c.state='DESCARTADO'")]
    return out


def public_config() -> dict:
    cfg = config.load()
    pin = bool(cfg["acesso_remoto"].get("pin_hash"))
    cfg["acesso_remoto"] = {k: v for k, v in cfg["acesso_remoto"].items() if k != "pin_hash"}
    cfg["acesso_remoto"]["pin_definido"] = pin
    return cfg


def mark_downloaded(export_id: str) -> None:
    with db.tx() as c:
        c.execute("UPDATE exports SET downloaded_at=COALESCE(downloaded_at, ?), "
                  "download_count=COALESCE(download_count, 0)+1 WHERE id=?", (config.iso(), export_id))
    log.event(item_id=export_id, agent="usuario", requested="baixar vídeo final", result="Vídeo Baixado")


def ready_list() -> list[dict]:
    """Cortes finalizados para você baixar e postar (celular ou PC)."""
    rows = db.query("SELECT e.id, e.title, e.description, e.hashtags, e.scheduled_for, e.qc_status, e.folder, "
                    "p.state AS pub_state, p.url AS pub_url, COALESCE(v.language, t.language) AS language FROM exports e "
                    "LEFT JOIN publications p ON p.export_id=e.id LEFT JOIN cuts c ON c.id=e.cut_id "
                    "LEFT JOIN videos v ON v.id=c.video_id LEFT JOIN transcripts t ON t.video_id=c.video_id "
                    "WHERE e.id NOT LIKE 'exp_pg%' ORDER BY e.scheduled_for")  # vídeos de página têm seção própria
    for r in rows:
        r["video_url"] = f"/media/final/{r['id']}"
        e = db.get("exports", r["id"]) or {}
        r["marcacoes"] = pos_postagem.marks(r["id"])
        r["exclusao_prevista"] = pos_postagem.due_date(r["id"])
        r["video_excluido_em"], r["nuvem"] = e.get("video_deleted_at"), e.get("cloud_path")
        r["baixado_em"], r["vezes_baixado"] = e.get("downloaded_at"), e.get("download_count") or 0
        desc = Path(r["folder"]) / "descricao.txt" if r["folder"] else None
        r["descricao_completa"] = desc.read_text("utf-8") if desc and desc.exists() else r["description"]
        del r["folder"]
    return rows


class Handler(BaseHTTPRequestHandler):
    server_version = "cortes/0.1"

    def log_message(self, *a):  # silencioso: os logs relevantes vão para o banco
        pass

    def _json(self, obj, code=200, cookie: str | None = None):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path):
        if not path.exists() or not path.is_file():
            return self._json({"erro": "não encontrado"}, 404)
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        rng = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
        start, end = 0, size - 1
        if rng and (rng.group(1) or rng.group(2)):  # Range: necessário para avançar/voltar no player
            if rng.group(1):
                start = int(rng.group(1))
                end = min(int(rng.group(2)), size - 1) if rng.group(2) else size - 1
            else:  # sufixo: últimos N bytes
                start = max(0, size - int(rng.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with path.open("rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 16, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (ConnectionResetError, BrokenPipeError):
                    return
                left -= len(chunk)

    def _host(self) -> str:
        h = self.headers.get("Host") or ""
        return h[1:h.index("]")] if h.startswith("[") and "]" in h else h.rsplit(":", 1)[0]

    PUBLIC = ("/", "/index.html", "/api/sessao", "/api/login")

    def _access(self, path: str) -> str | None:
        """'local' (o próprio PC), 'remote' (celular autenticado) ou None (resposta de erro já enviada)."""
        host, client = self._host(), self.client_address[0]
        if remote.is_local(client, host, self.headers):
            return "local"
        if not remote.settings()["habilitado"]:
            self._json({"erro": "acesso remoto desligado"}, 403)
            return None
        if not remote.network_allowed(client) or not remote.host_allowed(host):  # anti DNS rebinding
            self._json({"erro": "origem não permitida"}, 403)
            return None
        if path in self.PUBLIC or path.startswith("/static/") or remote.valid_session(self.headers.get("Cookie")):
            return "remote"
        self._json({"erro": "faça login com o PIN"}, 401)
        return None

    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        who = self._access(p)
        if not who:
            return
        if p == "/api/sessao":
            return self._json({"local": who == "local",
                               "autenticado": who == "local" or remote.valid_session(self.headers.get("Cookie"))})
        if p.startswith("/media/final/"):
            e = db.get("exports", p.rsplit("/", 1)[-1])
            if not e or not e["video_path"] or not Path(e["video_path"]).exists():
                return self._json({"erro": "vídeo não está mais no computador"}, 404)
            if q.get("baixar"):  # botão "Baixar vídeo": registra para mostrar "Vídeo Baixado"
                mark_downloaded(e["id"])
            return self._file(Path(e["video_path"]))
        if p == "/api/remoto" and who == "local":
            port = self.server.server_address[1]
            return self._json({**public_config()["acesso_remoto"], **remote.addresses(port),
                               "escutando_na_rede": self.server.server_address[0] != "127.0.0.1"})
        if p in ("/", "/index.html"):
            return self._file(UI / "index.html")
        if p.startswith("/static/"):
            f = (UI / p[len("/static/"):]).resolve()
            return self._file(f) if f.is_relative_to(UI.resolve()) else self._json({"erro": "proibido"}, 403)
        if p.startswith("/media/pagina/") or p.startswith("/media/pagina-capa/"):
            pv = db.get("page_videos", p.rsplit("/", 1)[-1])
            f = pv and pv.get("cover" if "capa" in p else "final_path")
            return self._file(Path(f)) if f and Path(f).exists() else self._json({"erro": "não disponível"}, 404)
        if p.startswith("/media/preview/"):
            c = db.get("cuts", p.rsplit("/", 1)[-1])
            return self._file(Path(c["preview_path"])) if c and c["preview_path"] else self._json({"erro": "sem prévia"}, 404)
        routes = {"/api/queue/opportunities": opp_queue, "/api/queue/cuts": cut_queue, "/api/dashboard": dashboard,
                  "/api/prontos": ready_list, "/api/relatorios": report_list, "/api/arquivo": archive, "/api/producao": progress.snapshot,
                  "/api/pagina": lambda: {"itens": pagina.listing(), "foco": config.load().get("foco") or ""},
                  "/api/claude-code": lambda: llm.status(refresh="verificar=1" in (self.path.split("?", 1) + [""])[1]),
                  "/api/meta": lambda: {"botoes": decisions.BUTTONS, "motivos": decisions.REASONS}}
        if p == "/api/stats":
            since = (q.get("since") or [None])[0]
            return self._json({k: decisions.stats(k, since) for k in ("opportunity", "cut")})
        if p in routes:
            return self._json(routes[p]())
        return self._json({"erro": "rota inexistente"}, 404)

    def do_POST(self):
        p = urlparse(self.path).path
        who = self._access(p)
        if not who:
            return
        # contra requisições forjadas por outros sites: cabeçalho próprio + mesma origem
        origin = self.headers.get("Origin")
        if self.headers.get("X-Cortes") != "1" or (origin and urlparse(origin).hostname != self._host()):
            return self._json({"erro": "origem não permitida"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 <= n <= 1_000_000:
                raise ValueError("tamanho de requisição inválido")
            body = json.loads(self.rfile.read(n) or b"{}")
            if p == "/api/login":
                token = remote.login(self.client_address[0], str(body.get("pin", "")))
                if not token:
                    return self._json({"erro": "PIN incorreto"}, 401)
                return self._json({"ok": True}, cookie=f"cortes_sessao={token}; HttpOnly; SameSite=Strict; Path=/; "
                                                       f"Max-Age={remote.SESSION_TTL}")
            if p in ("/api/config", "/api/pin") and who != "local":
                return self._json({"erro": "só pode ser alterado no próprio computador"}, 403)
            if p == "/api/pin":
                remote.set_pin(str(body["pin"]))
                return self._json({"ok": True})
            if p == "/api/postado" and body.get("plataforma"):  # TikTok / Instagram (marca ou desmarca)
                return self._json(pos_postagem.mark(body["export_id"], body["plataforma"], body.get("url"),
                                                    bool(body.get("desfazer"))))
            if p == "/api/pagina/buscar":  # novo lote de Vídeos de Página (sempre com a sua aprovação)
                return self._json({"ok": True, "lote": pagina.request(int(body.get("quantidade") or 5))})
            if p == "/api/pagina/decidir":
                return self._json(pagina.decide(str(body["id"]), int(body["score"])))
            if p == "/api/pesquisar":  # pedido de assuntos feito na Central (todos os campos opcionais)
                q, mv = body.get("quantidade"), body.get("min_visualizacoes")
                item = orchestrator.request_research(str(body.get("tema") or ""), str(body.get("categoria") or ""),
                                                     int(q) if q not in (None, "") else None,
                                                     int(mv) if mv not in (None, "") else None)
                return self._json({"ok": True, "pedido": item})
            if p == "/api/postado":  # você postou manualmente: registra sem alegar integração automática
                e = db.get("exports", body["export_id"])
                if not e:
                    return self._json({"erro": "não encontrado"}, 404)
                pub = db.query("SELECT id FROM publications WHERE export_id=?", (e["id"],))
                data = {"state": "POSTADO_PELO_USUARIO", "url": (body.get("url") or "").strip() or None,
                        "published_at": config.iso(), "error": None}
                if pub:
                    db.update("publications", pub[0]["id"], data)
                else:
                    db.insert("publications", {"id": db.new_id("pub"), "export_id": e["id"], "platform": e["platform"],
                                               "idem_key": f"{e['id']}:{e['platform']}", **data})
                db.update("exports", e["id"], {"state": "POSTADO_PELO_USUARIO"})
                return self._json({"ok": True})
            if p == "/api/decide":
                return self._json(decisions.decide(body["kind"], body["id"], int(body["score"]), body.get("reason")))
            if p == "/api/undo":
                return self._json(decisions.undo(int(body["decision_id"])))
            if p == "/api/reconsider":
                table = decisions.TABLE[body["kind"]]
                item = db.get(table, body["id"])
                if not item or item["state"] != "ADIADO":
                    return self._json({"erro": "só itens adiados podem voltar à fila"}, 400)
                db.update(table, body["id"], {"state": "AGUARDANDO_APROVACAO"})
                return self._json({"ok": True})
            if p == "/api/config":
                new = body["config"]
                new.setdefault("acesso_remoto", {})
                new["acesso_remoto"].pop("pin_definido", None)
                new["acesso_remoto"]["pin_hash"] = config.load()["acesso_remoto"].get("pin_hash", "")  # nunca via JSON
                config.save(new)
                return self._json({"ok": True})
        except (KeyError, ValueError, PermissionError) as e:
            return self._json({"erro": str(e)}, 400)
        return self._json({"erro": "rota inexistente"}, 404)


def serve(port: int = 8770, worker: bool = True) -> None:
    config.ensure_dirs()
    db.conn()
    stop = threading.Event()
    ThreadingHTTPServer.allow_reuse_address = False  # no Windows, reuse permitiria 2 servidores na mesma porta
    # sem acesso remoto, escuta só no próprio PC; com ele, na rede (filtrada por IP + PIN em Handler._access)
    bind = "0.0.0.0" if config.load()["acesso_remoto"]["habilitado"] else "127.0.0.1"
    httpd = ThreadingHTTPServer((bind, port), Handler)  # bind antes de ligar o gerente
    if worker:
        threading.Thread(target=orchestrator.loop, args=(stop,), daemon=True, name="gerente").start()
    print(f"Interface: http://127.0.0.1:{port}  (feche esta janela para encerrar tudo)")
    if bind != "127.0.0.1":
        for kind, urls in remote.addresses(port).items():
            if isinstance(urls, list) and urls:
                print(f"Celular ({kind}): {', '.join(urls)}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
