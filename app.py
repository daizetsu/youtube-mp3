"""YouTube -> MP3: recebe um link do YouTube e devolve o áudio em MP3 320 kbps.

Ao abrir o executável, o servidor sobe em segundo plano e uma janela própria
(Microsoft Edge em "modo aplicativo") é aberta. Fechar a janela encerra o programa.
"""
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser

# Sem console (--noconsole) stdout/stderr ficam None; evita erros de log.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

from flask import Flask, after_this_request, jsonify, render_template, request, send_file
import yt_dlp

# Quando empacotado como .exe (PyInstaller), os arquivos ficam em sys._MEIPASS
BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
FFMPEG_DIR = os.path.join(BASE, "ffmpeg")
if os.path.isdir(FFMPEG_DIR):
    # ffmpeg e deno (usado pelo yt-dlp para o YouTube) embutidos no executável
    os.environ["PATH"] = FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")

PORTA_PADRAO = 8765
ASSINATURA = "youtube-mp3-ok"

app = Flask(__name__, template_folder=os.path.join(BASE, "templates"),
            static_folder=os.path.join(BASE, "static"))

YOUTUBE_RE = re.compile(r"^(https?://)?(www\.|m\.|music\.)?(youtube\.com|youtu\.be)/", re.I)

# Controle de "janela aberta": a página envia /ping periodicamente.
_estado = {"ultimo_ping": None, "convertendo": 0}
_lock = threading.Lock()


def baixar_mp3(url: str, pasta: str) -> tuple[str, str]:
    """Baixa o melhor áudio disponível e converte para MP3 320 kbps."""
    opcoes = {
        "format": "bestaudio/best",          # melhor stream de áudio disponível
        "outtmpl": os.path.join(pasta, "%(title)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",   # bitrate máximo do MP3
            },
            {"key": "FFmpegMetadata"},        # título/artista no arquivo
            {"key": "EmbedThumbnail"},        # capa do vídeo no MP3
        ],
        "writethumbnail": True,
    }
    if os.path.isdir(FFMPEG_DIR):
        opcoes["ffmpeg_location"] = FFMPEG_DIR
    with yt_dlp.YoutubeDL(opcoes) as ydl:
        info = ydl.extract_info(url, download=True)
        titulo = info.get("title") or "audio"

    mp3s = [f for f in os.listdir(pasta) if f.endswith(".mp3")]
    if not mp3s:
        raise RuntimeError("A conversão não gerou o arquivo MP3.")
    return os.path.join(pasta, mp3s[0]), titulo


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/ping")
def ping():
    with _lock:
        _estado["ultimo_ping"] = time.time()
    return ASSINATURA


@app.post("/converter")
def converter():
    url = (request.form.get("url") or "").strip()
    if not YOUTUBE_RE.match(url):
        return jsonify(erro="Cole um link válido do YouTube."), 400

    pasta = tempfile.mkdtemp(prefix="ytmp3_")
    with _lock:
        _estado["convertendo"] += 1
    try:
        caminho, titulo = baixar_mp3(url, pasta)
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(pasta, ignore_errors=True)
        msg = str(e).replace("ERROR: ", "")
        return jsonify(erro=f"Não foi possível converter: {msg}"), 500
    finally:
        with _lock:
            _estado["convertendo"] -= 1
            _estado["ultimo_ping"] = time.time()

    @after_this_request
    def limpar(resposta):
        resposta.call_on_close(lambda: shutil.rmtree(pasta, ignore_errors=True))
        return resposta

    nome = re.sub(r'[\\/:*?"<>|]+', "", titulo).strip() or "audio"
    return send_file(caminho, mimetype="audio/mpeg", as_attachment=True,
                     download_name=f"{nome}.mp3")


# ---------------------------------------------------------------- área de trabalho

def ja_esta_rodando(porta: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{porta}/ping", timeout=1) as r:
            return r.read().decode() == ASSINATURA
    except Exception:  # noqa: BLE001
        return False


def porta_livre(preferida: int) -> int:
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", preferida)) != 0:
            return preferida
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def achar_navegador() -> str | None:
    """Edge vem em todo Windows 10/11; Chrome como alternativa."""
    candidatos = []
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            candidatos += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                           os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    for c in candidatos:
        if os.path.exists(c):
            return c
    return shutil.which("msedge") or shutil.which("chrome") or shutil.which("chromium")


def abrir_janela(endereco: str) -> subprocess.Popen | None:
    nav = achar_navegador()
    if not nav:
        webbrowser.open(endereco)
        return None
    perfil = os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
                          "YouTube-MP3", "janela")
    os.makedirs(perfil, exist_ok=True)
    return subprocess.Popen([
        nav, f"--app={endereco}", f"--user-data-dir={perfil}",
        "--window-size=560,520", "--no-first-run", "--no-default-browser-check",
        "--disable-background-mode", "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
    ])


def vigiar(processo: subprocess.Popen | None) -> None:
    """Encerra o programa quando a janela é fechada."""
    inicio = time.time()
    while True:
        time.sleep(2)
        with _lock:
            ultimo, ocupado = _estado["ultimo_ping"], _estado["convertendo"]
        if ocupado:
            continue
        # Sem sinal da página por muito tempo (ou nunca abriu) => janela fechada.
        referencia = ultimo or inicio
        limite = 150 if ultimo else 120
        if time.time() - referencia > limite:
            os._exit(0)
        # Se nós abrimos a janela e o processo dela terminou, sai logo.
        if processo is not None and processo.poll() is not None and ultimo \
                and time.time() - ultimo > 10:
            os._exit(0)


def main() -> None:
    if "--servidor" in sys.argv:  # modo servidor puro (ex.: uso futuro via Tailscale)
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", PORTA_PADRAO)))
        return

    if ja_esta_rodando(PORTA_PADRAO):
        abrir_janela(f"http://127.0.0.1:{PORTA_PADRAO}")
        return

    porta = porta_livre(PORTA_PADRAO)
    endereco = f"http://127.0.0.1:{porta}"
    servidor = threading.Thread(target=lambda: app.run(host="127.0.0.1", port=porta),
                                daemon=True)
    servidor.start()
    for _ in range(50):  # espera o servidor responder
        if ja_esta_rodando(porta):
            break
        time.sleep(0.1)
    vigiar(abrir_janela(endereco))


if __name__ == "__main__":
    main()
