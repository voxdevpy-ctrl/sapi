"""Gera o ícone do SAPI (engrenagem de latão sobre ferro) em PNG, só com a biblioteca padrão.

Uso: python ferramentas/gerar_icone.py  → cortes/ui/icone.png e assets/avatar-pagina.png (256 px).
Para o .ico do atalho: ffmpeg -i assets/avatar-pagina.png assets/sapi.ico
"""
import math
import struct
import zlib
from pathlib import Path

N, SS = 256, 3  # tamanho final e supersampling (anti-serrilhado)
FERRO, LATAO_CLARO, LATAO_ESCURO, BORDA = (24, 27, 32), (241, 208, 126), (169, 127, 43), (59, 66, 77)


def cor(x: float, y: float):
    """Cor de um ponto em coordenadas 0..1 (None = transparente)."""
    # fundo: quadrado de cantos arredondados
    r = 0.18
    dx, dy = max(abs(x - .5) - (.5 - r), 0), max(abs(y - .5) - (.5 - r), 0)
    if dx * dx + dy * dy > r * r:
        return None
    borda = dx * dx + dy * dy > (r - .025) ** 2 or min(x, y, 1 - x, 1 - y) < .025
    base = BORDA if borda else FERRO
    # engrenagem: anel + 8 dentes + furo central
    cx, cy = x - .5, y - .5
    d, a = math.hypot(cx, cy), math.atan2(cy, cx)
    dente = (math.cos(a * 8) > .35)
    raio_ext = .335 if dente else .27
    if .13 < d < raio_ext or d < .055:
        t = (y - .15) / .7  # degradê de latão de cima para baixo
        return tuple(round(LATAO_CLARO[i] + (LATAO_ESCURO[i] - LATAO_CLARO[i]) * min(max(t, 0), 1)) for i in range(3)) + (255,)
    return base + (255,)


def png(caminho: Path) -> None:
    linhas = []
    for j in range(N):
        linha = bytearray([0])
        for i in range(N):
            acc, cont = [0, 0, 0, 0], 0
            for sj in range(SS):
                for si in range(SS):
                    c = cor((i + (si + .5) / SS) / N, (j + (sj + .5) / SS) / N)
                    if c:
                        for k in range(4):
                            acc[k] += c[k]
                    cont += 1
            a = acc[3] / cont
            linha += bytes([round(acc[k] / max(1, acc[3] / 255)) if acc[3] else 0 for k in range(3)] + [round(a)])
        linhas.append(bytes(linha))
    raw = zlib.compress(b"".join(linhas), 9)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", N, N, 8, 6, 0, 0, 0))
                        + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


if __name__ == "__main__":
    raiz = Path(__file__).resolve().parent.parent
    for destino in (raiz / "cortes" / "ui" / "icone.png", raiz / "assets" / "avatar-pagina.png"):
        png(destino)
        print("gerado:", destino.relative_to(raiz))
