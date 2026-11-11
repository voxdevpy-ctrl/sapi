"""Caminhos, relógio controlável e configuração (padrões + arquivo do usuário)."""
from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _ler_env(arquivo: Path) -> dict:
    """Lê o .env local (opcional) só com as chaves conhecidas; variáveis do sistema têm prioridade."""
    conhecidas = {"SAPI_DADOS", "CORTES_PRODUCAO"}
    out = {}
    try:
        for linha in arquivo.read_text("utf-8").splitlines():
            k, sep, v = linha.strip().partition("=")
            if sep and k.strip() in conhecidas and not linha.lstrip().startswith("#"):
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


_ARQ = _ler_env(ROOT / ".env")
# pasta de dados desta instalação (banco, vídeos, logs, configuração). Nunca vai para o repositório.
# Variável do sistema vence o .env (os testes dependem disso para nunca tocar nos dados reais).
PROD = Path(os.environ.get("SAPI_DADOS") or os.environ.get("CORTES_PRODUCAO")
            or _ARQ.get("SAPI_DADOS") or _ARQ.get("CORTES_PRODUCAO") or ROOT / "PRODUCAO").expanduser()

DIRS = {
    "originais": PROD / "ORIGINAIS" / "TEMPORARIOS_14_DIAS",
    "produzidos": PROD / "PRODUZIDOS",
    "descartados": PROD / "DESCARTADOS",
    "adiados": PROD / "ADIADOS",
    "intermediarios": PROD / "INTERMEDIARIOS",
    "logs": PROD / "LOGS",
    "relatorios": PROD / "RELATORIOS",
    "config": PROD / "CONFIGURACOES",
    "entrada": PROD / "ENTRADA_LOCAL",  # vídeos próprios/autorizados colocados pelo usuário
}

DEFAULTS: dict = {
    "retencao_dias": 14,
    "janela_desfazer_s": 90,           # o worker só age em decisões mais antigas que isso
    "max_tentativas": 3,
    "hardware": {"threads": 3, "ram_minima_mb": 1200, "disco_margem_mb": 2048},
    "compressao": {"codec": "libx264", "preset": "veryfast", "crf": 24, "altura_max": 1080,
                   "audio_kbps": 128, "economia_minima": 0.15,
                   "pular_abaixo_mbps": 1.0},  # abaixo disso o encode não compensa (medido)
    # "auto" = o Whisper detecta o idioma falado (necessário para fontes estrangeiras)
    "transcricao": {"modelo": "small", "idioma": "auto", "compute_type": "int8", "beam_size": 1,
                    "limiar_vozes": 0.75},  # 0,6 dividia 2 pessoas em até 24 vozes; 0,75 acertou (testado)
    # legendas de vídeos em outro idioma são traduzidas para o idioma de destino
    "traducao": {"habilitada": True, "idioma_destino": "pt"},
    "cortes": {"duracao_min": 20, "duracao_max": 90, "max_por_video": 6, "versao_criterios": "v1",
               "pesos": {"clareza": 1.5, "interesse": 1.3, "narrativa": 1.2, "inicio": 1.0,
                         "atencao": 1.0, "encerramento": 1.0, "sem_repeticao": 0.6,
                         "audiovisual": 0.6, "compartilhamento": 0.8, "perfil": 0.8}},
    # ritmo (10/11/2026): corta o silêncio inútil e mantém só uma pausa curta, que varia com o contexto.
    # Valores em segundos = quanto de pausa FICA. Pausa dramática = depois de "?", "!", "…" ou ":".
    "ritmo": {"versao": 2, "pausa_meio_frase": 0.15, "pausa_fim_frase": 0.4, "pausa_dramatica": 0.85,
              "pausa_troca_locutor": 0.25, "pausa_pergunta_resposta": 0.5, "min_removido": 0.2,
              "min_trecho": 0.35, "folga_palavra": 0.06, "silencio_db": -35},
    # cota (10/11/2026): tudo em PRODUCAO. No limite a produção pausa; a tolerância só termina o que já rodava
    "armazenamento": {"limite_gb": 10, "tolerancia_gb": 1},
    # "pagina" = Central só trabalha nos Vídeos de Página até o lote acabar (depois volta sozinha ao normal)
    "foco": "",
    # foco "tema": só trabalha em pesquisas, vídeos e cortes cujo assunto/título contém um destes termos
    "foco_termos": [],
    "pagina": {"nome": "Minha Página", "arroba": "@minhapagina", "avatar": "", "duracao_min_s": 6, "duracao_max_s": 75,
               "min_visualizacoes": 300000, "recorte_max": 0.32,
               # vídeo feito por IA ou muito repostado só entra se for MUITO viral (pedido do usuário, 10/11/2026)
               "ia_repost_min_visualizacoes": 5_000_000,
               # ...e se estiver no topo do engajamento do lote (os X% melhores em curtidas+comentários por mil)
               "ia_repost_topo_pct": 25,
               # ...e nunca abaixo deste piso fixo de engajamento (curtidas + 5×comentários por mil visualizações)
               "ia_repost_piso_por_mil": 15,
               # canais que entram só pela mesma regra da IA (título de humor como isca; avaliado em 10/11/2026)
               "canais_regra_ia": [],
               # brasileiros primeiro; gringo só com engajamento >= este fator × a mediana do lote
               "gringo_fator_mediana": 2.0,
               # piso geral: abaixo disso o vídeo não entra; lote fraco mede mais "enriquecer_extra" candidatos
               "engajamento_min_por_mil": 8,
               "enriquecer_extra": 40,
               # o caçador vê a miniatura dos N candidatos mais vistos (para perceber IA e marca d'água)
               "miniaturas_cacador": 40,
               # antes de escolher, puxa os dados completos (comentários, curtidas, canal, duração, data) dos N mais vistos
               "enriquecer_top": 60,
               # 0 = sem mínimo: comentários e curtidas só dão prioridade
               "min_comentarios": 0,
               # aba Shorts das páginas de hashtag do YouTube (a busca comum quase não traz Shorts)
               "consultas": ["#humorbrasileiro", "#memesbrasileiros", "#engraçado", "#comedia", "#zueira",
                             "#resenha", "#humor", "#memes"],
               "exemplos_tom": ["meu maior arrependimento foi não ter seguido essa página antes kkkkk",
                                "quando a mãe fala \"vem cá rapidinho\" e já são 3 horas depois kkkkk",
                                "eu explicando pro meu chefe por que cheguei atrasado pela 4ª vez"]},
    "previa": {"altura": 480, "crf": 30},
    "final": {"formato": "vertical", "largura": 1080, "altura": 1920, "crf": 21, "preset": "veryfast",
              "loudnorm": "I=-14:TP=-1.5:LRA=11", "reducao_ruido": False,
              # 28 caracteres × 58 px × ~0,55 ≈ 890 px: cabe nos 960 px úteis da tela vertical
              "legenda": {"fonte": "Arial", "tamanho": 58, "margem_v": 480, "max_chars": 28}},
    "agenda": {"horarios": ["12:00", "19:00"], "plataforma": "youtube_shorts"},
    "publicacao": {"adaptador": "manual"},   # 'manual' = só prepara; integrações exigem OAuth
    "pesquisa": {"intervalo_h": 24, "max_oportunidades": 12, "consultas": [], "canais": [],
                 "geo": "BR", "nichos": [], "tendencias": True, "duracao_min_fonte_s": 300,
                 # 0 = sem limite; lives muito longas pesam no download/transcrição (padrão: até 4 h)
                 "duracao_max_fonte_s": 14400, "min_visualizacoes": 0,
                 # prioridade PT-BR: estrangeiro só se MUITO viral, e no máximo 2 por pesquisa
                 "idioma": {"principal": "pt", "estrangeiro_min_visualizacoes": 1_000_000,
                            "estrangeiro_max_por_pesquisa": 2}},
    "autorizacao": {"aceitar_creative_commons": True, "canais_autorizados": {}, "canais_proprios": [],
                    # decisão explícita do usuário: libera qualquer canal (registrada em cada vídeo)
                    "autorizar_qualquer_canal": False, "autorizado_em": ""},
    # vídeo final apagado no dia seguinte à(s) marcação(ões) de postado; textos e legendas ficam
    "pos_postagem": {"excluir_video": True, "exigir": "ambas"},
    # cópia opcional na nuvem (rclone) antes de apagar; se falhar, o vídeo não é apagado
    "nuvem": {"habilitada": False, "rclone_remote": "", "pasta": "CentralDeCortes"},
    "recomendacao": {"meia_vida_dias": 30, "suavizacao": 3.0, "exploracao_min": 0.15,
                     "exploracao_max": 0.5, "piso_segunda_chance": 0.15},
    "llm": {"habilitado": True, "timeout_s": 900, "claude_exe": ""},
    # celular: desligado por padrão; quando ligado, só rede Tailscale (e rede local se permitido) + PIN
    "acesso_remoto": {"habilitado": False, "porta": 8770, "permitir_rede_local": False,
                      "hosts_permitidos": [], "pin_hash": ""},
}


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load() -> dict:
    f = DIRS["config"] / "config.json"
    user = json.loads(f.read_text("utf-8")) if f.exists() else {}
    return _merge(DEFAULTS, user)


def _check_types(default, new, path="") -> None:
    for k, v in new.items():
        if k in default and default[k] is not None:
            d = default[k]
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) if isinstance(d, (int, float)) and not isinstance(d, bool) \
                else isinstance(v, type(d))
            if not ok:
                raise ValueError(f"'{path}{k}' deve ser {type(d).__name__}")
            if isinstance(d, dict) and k not in ("canais_autorizados", "pesos"):
                _check_types(d, v, f"{path}{k}.")


def save(new: dict) -> None:
    """Valida tipos contra os padrões e grava de forma atômica (o gerente nunca lê JSON pela metade)."""
    if not isinstance(new, dict):
        raise ValueError("configuração deve ser um objeto JSON")
    _check_types(DEFAULTS, new)
    f = DIRS["config"] / "config.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(new, indent=2, ensure_ascii=False), "utf-8")
    os.replace(tmp, f)


def ensure_dirs() -> None:
    for d in DIRS.values():
        d.mkdir(parents=True, exist_ok=True)
    f = DIRS["config"] / "config.json"
    if not f.exists():
        f.write_text(json.dumps(DEFAULTS, indent=2, ensure_ascii=False), "utf-8")


def now() -> datetime:
    """Hora local com fuso. CORTES_FAKE_NOW (ISO) permite simular datas nos testes
    sem alterar o relógio do computador."""
    fake = os.environ.get("CORTES_FAKE_NOW")
    if fake:
        dt = datetime.fromisoformat(fake)
        return dt if dt.tzinfo else dt.astimezone()
    return datetime.now().astimezone()


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


__all__ = ["ROOT", "PROD", "DIRS", "load", "ensure_dirs", "now", "iso", "parse", "timedelta"]
