"""Acesso pelo celular: desligado por padrão, PIN obrigatório, nada sensível exposto."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from cortes import config, db, remote, web

from .base import Base

REMOTO = {"X-Forwarded-For": "100.101.102.103"}  # requisição repassada (ex.: tailscale serve) = remota


class AcessoRemoto(Base):
    def setUp(self):
        super().setUp()
        remote._sessions.clear()
        remote._fails.clear()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def req(self, path, headers=None, body=None):
        h = {"Content-Type": "application/json", "X-Cortes": "1", **(headers or {})} if body is not None else dict(headers or {})
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=h,
                                   data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read() or b"null") if "json" in resp.headers.get("Content-Type", "") else None, resp.headers
        except urllib.error.HTTPError as e:
            e.read()
            e.close()
            return e.code, None, e.headers

    def enable(self, pin="482915"):
        remote.set_pin(pin)
        cfg = config.load()
        cfg["acesso_remoto"]["habilitado"] = True
        config.save(cfg)

    def test_desligado_por_padrao(self):
        self.assertEqual(self.req("/api/dashboard", REMOTO)[0], 403)
        self.assertEqual(self.req("/api/dashboard")[0], 200, "o próprio PC sempre acessa")

    def test_pin_obrigatorio_e_sessao(self):
        self.enable()
        self.assertEqual(self.req("/api/dashboard", REMOTO)[0], 401)
        self.assertEqual(self.req("/", REMOTO)[0], 200, "a página de login carrega")
        self.assertEqual(self.req("/api/login", REMOTO, {"pin": "000000"})[0], 401)
        code, _, h = self.req("/api/login", REMOTO, {"pin": "482915"})
        self.assertEqual(code, 200)
        cookie = h["Set-Cookie"].split(";")[0]
        self.assertIn("HttpOnly", h["Set-Cookie"])
        auth = {**REMOTO, "Cookie": cookie}
        self.assertEqual(self.req("/api/dashboard", auth)[0], 200)
        self.assertEqual(self.req("/api/config", auth, {"config": config.load()})[0], 403, "config só no PC")
        self.assertEqual(self.req("/api/pin", auth, {"pin": "111111"})[0], 403, "PIN só no PC")

    def test_bloqueio_por_tentativas(self):
        self.enable()
        for _ in range(5):
            self.req("/api/login", REMOTO, {"pin": "999999"})
        self.assertEqual(self.req("/api/login", REMOTO, {"pin": "482915"})[0], 400, "bloqueado mesmo com PIN certo")

    def test_pin_nunca_exposto_nem_sobrescrito_pelo_json(self):
        self.enable()
        _, d, _ = self.req("/api/dashboard")
        self.assertNotIn("pin_hash", json.dumps(d))
        self.assertTrue(d["config"]["acesso_remoto"]["pin_definido"])
        self.assertEqual(self.req("/api/config", body={"config": d["config"]})[0], 200)
        self.assertTrue(remote.check_pin("482915"), "salvar a configuração não apaga o PIN")
        self.assertTrue(config.load()["acesso_remoto"]["pin_hash"].startswith("pbkdf2$"))

    def test_pin_fraco_recusado(self):
        with self.assertRaises(ValueError):
            remote.set_pin("1234")

    def test_rede_e_host_fora_da_lista(self):
        self.enable()
        self.assertFalse(remote.network_allowed("192.168.100.20"), "Wi-Fi só se permitido")
        self.assertTrue(remote.network_allowed("100.90.1.2"))
        self.assertFalse(remote.network_allowed("8.8.8.8"))
        self.assertEqual(self.req("/api/sessao", {**REMOTO, "Host": "evil.example"})[0], 403)

    def test_prontos_e_postado(self):
        f = config.DIRS["produzidos"] / "2026-11-10" / "12-00"
        f.mkdir(parents=True)
        (f / "video.mp4").write_bytes(b"x" * 2000)
        (f / "descricao.txt").write_text("Descrição #tag", "utf-8")
        db.insert("exports", {"id": "e1", "cut_id": "c1", "video_path": str(f / "video.mp4"), "folder": str(f),
                              "title": "Título", "platform": "youtube_shorts", "scheduled_for": "2026-11-10T12:00:00-03:00"})
        _, rows, _ = self.req("/api/prontos")
        self.assertEqual((rows[0]["title"], rows[0]["descricao_completa"]), ("Título", "Descrição #tag"))
        self.assertEqual(self.req("/media/final/e1", {"Range": "bytes=0-9"})[0], 206)
        self.assertEqual(self.req("/api/postado", body={"export_id": "e1", "url": "https://exemplo/abc"})[0], 200)
        pub = db.query("SELECT state, url FROM publications")[0]
        self.assertEqual((pub["state"], pub["url"]), ("POSTADO_PELO_USUARIO", "https://exemplo/abc"))


class ClaudeCodeStatus(Base):
    """Conectar Claude Code: cada máquina usa o próprio; nada de credencial é lido ou exposto."""

    def test_nao_instalado_orienta(self):
        from unittest import mock
        from cortes import llm
        with mock.patch.object(llm, "claude_exe", side_effect=llm.LLMError("não encontrado")):
            s = llm.status(refresh=True)
        self.assertFalse(s["instalado"])
        self.assertIn("Instale", s["orientacao"])

    def test_instalado_com_versao_sem_ler_credencial(self):
        import subprocess
        from pathlib import Path
        from unittest import mock
        from cortes import llm
        exe = str(Path.home() / "bin" / "claude.exe")
        orig = Path.read_text
        fake = subprocess.CompletedProcess([exe], 0, stdout="9.9.9 (Claude Code)\n", stderr="")
        with mock.patch.object(llm, "claude_exe", return_value=exe), mock.patch("subprocess.run", return_value=fake), \
                mock.patch.object(Path, "read_text", autospec=True, side_effect=lambda self, *a, **k: (
                    (_ for _ in ()).throw(AssertionError("não pode ler credenciais")) if self.name == ".credentials.json"
                    else orig(self, *a, **k))):
            s = llm.status(refresh=True)
        self.assertTrue(s["instalado"])
        self.assertEqual(s["versao"], "9.9.9 (Claude Code)")
        self.assertTrue(s["caminho"].startswith("~"), "caminho sem o nome do usuário")


class PastaDeDados(Base):
    """Cada instalação usa a própria pasta de dados; variável do sistema vence o .env."""

    def test_le_env_so_chaves_conhecidas(self):
        import tempfile
        from pathlib import Path
        from cortes import config
        f = Path(tempfile.mkdtemp()) / ".env"
        f.write_text("# comentário\nSAPI_DADOS=D:\SAPI-dados\nOUTRA=segredo\n", "utf-8")
        self.assertEqual(config._ler_env(f), {"SAPI_DADOS": "D:\SAPI-dados"})
        self.assertEqual(config._ler_env(f.with_name("nao-existe")), {})

    def test_testes_nunca_usam_dados_reais(self):
        import os
        from cortes import config
        self.assertEqual(str(config.PROD), os.environ["SAPI_DADOS"])
        self.assertNotIn("PRODUCAO", str(config.PROD))
