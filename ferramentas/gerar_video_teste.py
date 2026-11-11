"""Gera um vídeo de TESTE autorizado (conteúdo sintético próprio): diálogo com duas vozes TTS do Windows.

Uso: python ferramentas/gerar_video_teste.py <saida.mp4> [pt|en]
Inclui apresentações nominais (evidência para identificação), pergunta/resposta, uma pausa longa
(teste de remoção de silêncio) e mudança de assunto.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

DIALOGO = [
    ("Daniel", "Olá, pessoal, bem-vindos a mais um episódio do nosso podcast. Eu sou o Daniel e hoje eu converso com uma convidada especial."),
    ("Maria", "Oi, Daniel, obrigada pelo convite. Meu nome é Maria, eu sou engenheira de software e trabalho com inteligência artificial há dez anos."),
    ("Daniel", "Maria, eu quero começar com uma pergunta que muita gente faz. Como foi que você começou a programar?"),
    ("Maria", "Olha, foi meio por acaso. Eu tinha quinze anos e o computador da minha casa quebrou. Ninguém sabia consertar, então eu fui pesquisar sozinha."),
    ("Maria", "Passei uma semana inteira lendo fóruns, testando coisas, e quando ele finalmente ligou de novo eu pensei: é isso que eu quero fazer da vida."),
    ("Daniel", "Que história incrível. E o que você aprendeu com essa experiência?"),
    ("Maria", "Aprendi que não saber uma coisa não é um problema. O problema é ter medo de tentar. Até hoje eu levo isso para o meu trabalho."),
    ("PAUSA", "3.5"),
    ("Daniel", "Agora vamos mudar de assunto. Muita gente tem medo de que a inteligência artificial substitua todos os empregos. O que você acha disso?"),
    ("Maria", "Eu acho que esse medo é compreensível, mas exagerado. A tecnologia muda as tarefas, não elimina a necessidade de pessoas. Quem aprende a usar as ferramentas sai na frente."),
    ("Maria", "Por exemplo, na minha equipe, a ferramenta faz a parte repetitiva, e as pessoas ficam com as decisões difíceis. O trabalho ficou mais interessante, não menor."),
    ("Daniel", "Então o conselho é aprender a usar, e não fugir da tecnologia."),
    ("Maria", "Exatamente. E começar pequeno. Uma hora por semana já faz diferença depois de alguns meses."),
    ("Daniel", "Maria, muito obrigado pela conversa. Foi um prazer enorme."),
    ("Maria", "Eu que agradeço, Daniel. Até a próxima."),
]

# Diálogo em inglês (teste de tradução): Emma = voz Zira (en-US), James = voz Daniel
DIALOGO_EN = [
    ("Emma", "Hi everyone, welcome back to the show. I'm Emma, and today I'm talking to a guest who changed careers late in life."),
    ("James", "Thanks for having me, Emma. My name is James, and I became a nurse when I was forty-five years old."),
    ("Emma", "James, that's a big move. What made you leave your old job?"),
    ("James", "Honestly, I was burned out. I spent twenty years selling insurance, and one day my father got sick. I watched the nurses take care of him, and I thought, that is real work."),
    ("James", "So I went back to school at night. It took me three years, and I almost quit twice. But I kept going."),
    ("Emma", "That sounds really tough. What kept you from giving up?"),
    ("James", "My daughter. She told me, Dad, you always tell me not to quit. So I couldn't exactly quit, could I?"),
    ("PAUSA", "3.5"),
    ("Emma", "Let's switch gears. A lot of people think it's too late to start over after forty. What would you tell them?"),
    ("James", "I'd say the fear is normal, but it lies to you. You're not starting from zero. Everything you learned before comes with you. I use my sales skills every day to calm scared patients."),
    ("Emma", "So your old career actually helps you now."),
    ("James", "Exactly. Nothing you learn is wasted. Start small, take one class, and see how it feels."),
    ("Emma", "James, thank you so much. This was wonderful."),
    ("James", "Thank you, Emma. Take care."),
]
VOZES = {"Daniel": "Microsoft Daniel", "Maria": "Microsoft Maria Desktop",
         "Emma": "Microsoft Zira Desktop", "James": "Microsoft Daniel"}


def main(out: Path, idioma: str = "pt"):
    tmp = Path(tempfile.mkdtemp(prefix="cortes_tts_"))
    lines = ["Add-Type -AssemblyName System.Speech", "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
             "$s.Rate = 0"]
    parts = []
    for i, (who, text) in enumerate(DIALOGO_EN if idioma == "en" else DIALOGO):
        if who == "PAUSA":
            parts.append(("pausa", float(text)))
            continue
        voice = VOZES[who]
        wav = tmp / f"{i:02d}.wav"
        t = text.replace("'", "''")
        lines += [f"$s.SelectVoice('{voice}')", f"$s.SetOutputToWaveFile('{wav}')", f"$s.Speak('{t}')", "$s.SetOutputToNull()"]
        parts.append(("wav", wav))
    ps1 = tmp / "tts.ps1"
    ps1.write_text("\n".join(lines), "utf-8-sig")
    # pwsh (PowerShell 7) enxerga as vozes OneCore (ex.: Daniel); o Windows PowerShell 5.1 não
    subprocess.run(["pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)], check=True)
    # concatena com pausas naturais entre falas (0,6 s) e a pausa longa marcada
    lst = tmp / "lista.txt"
    sil = {}
    entries = []
    for kind, val in parts:
        if kind == "pausa":
            d = val
        else:
            entries.append(val)
            d = 0.6
        if d not in sil:
            sil[d] = tmp / f"sil_{d}.wav"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
                            "-t", str(d), str(sil[d])], check=True)
        entries.append(sil[d])
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in entries), "utf-8")
    audio = tmp / "dialogo.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-ar", "44100", "-ac", "2", str(audio)], check=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
                    "-i", str(audio), "-shortest", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                    "-c:a", "aac", "-b:a", "192k", str(out)], check=True)
    print(out)


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "pt")
