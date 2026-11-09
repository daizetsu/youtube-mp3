"""YouTube -> MP3: converte vários links do YouTube em MP3 320 kbps, em paralelo.

Ao abrir o executável, o servidor sobe em segundo plano, um ícone aparece na
bandeja do Windows e uma janela própria (Edge em "modo aplicativo") é aberta.
Fechar a janela não encerra o programa: ele continua atendendo o celular pelo
Tailscale até você clicar em "Sair" no ícone da bandeja.
Os MP3 prontos são salvos automaticamente em Downloads/YouTube MP3.
"""
import io
import ipaddress
import itertools
import json
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
import zipfile
from concurrent.futures import ThreadPoolExecutor

# Sem console (--noconsole) stdout/stderr ficam None; evita erros de log.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

from flask import Flask, abort, jsonify, render_template, request, send_file
import yt_dlp

# Quando empacotado como .exe (PyInstaller), os arquivos ficam em sys._MEIPASS
BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
FFMPEG_DIR = os.path.join(BASE, "ffmpeg")
if os.path.isdir(FFMPEG_DIR):
    # ffmpeg e deno (usado pelo yt-dlp para o YouTube) embutidos no executável
    os.environ["PATH"] = FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")

PORTA_PADRAO = 8765
ASSINATURA = "youtube-mp3-ok"
# Conversões simultâneas: cada uma usa ~1 núcleo no ffmpeg + rede.
SIMULTANEOS = max(2, min(6, (os.cpu_count() or 4)))

app = Flask(__name__, template_folder=os.path.join(BASE, "templates"),
            static_folder=os.path.join(BASE, "static"))

# Só o próprio PC e aparelhos da sua rede Tailscale podem acessar.
REDES_PERMITIDAS = [ipaddress.ip_network(r) for r in (
    "127.0.0.0/8", "::1/128",
    "100.64.0.0/10", "fd7a:115c:a1e0::/48",       # faixas de endereço do Tailscale
)]

LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/[^\s,;\"'<>]+", re.I)


def pasta_saida() -> str:
    pasta = os.path.join(os.path.expanduser("~"), "Downloads", "YouTube MP3")
    try:
        os.makedirs(pasta, exist_ok=True)
        return pasta
    except OSError:
        pasta = os.path.join(tempfile.gettempdir(), "YouTube MP3")
        os.makedirs(pasta, exist_ok=True)
        return pasta


# ------------------------------------------------------------------- fila

_lock = threading.Lock()
_ids = itertools.count(1)
_itens: dict[int, dict] = {}            # id -> item (ordem de inserção)
_executor = ThreadPoolExecutor(max_workers=SIMULTANEOS, thread_name_prefix="conv")
_estado = {"ultimo_ping": None}

ATIVOS = ("fila", "baixando", "convertendo", "playlist")


def _novo_item(url: str, titulo: str | None = None, status: str = "fila") -> dict:
    item = {"id": next(_ids), "url": url, "titulo": titulo or url, "status": status,
            "progresso": 0, "erro": None, "arquivo": None, "nome": None}
    with _lock:
        _itens[item["id"]] = item
    return item


def _atualizar(item: dict, **campos) -> None:
    with _lock:
        item.update(campos)


def _nome_unico(pasta: str, nome: str) -> str:
    base, ext = os.path.splitext(nome)
    destino = os.path.join(pasta, nome)
    n = 2
    while os.path.exists(destino):
        destino = os.path.join(pasta, f"{base} ({n}){ext}")
        n += 1
    return destino


def _opcoes(pasta: str, item: dict) -> dict:
    def progresso(d):
        if d.get("info_dict", {}).get("title") and item["titulo"] == item["url"]:
            _atualizar(item, titulo=d["info_dict"]["title"])
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            pct = int(d.get("downloaded_bytes", 0) * 100 / total) if total else 0
            _atualizar(item, status="baixando", progresso=min(pct, 99))
        elif d["status"] == "finished":
            _atualizar(item, status="convertendo", progresso=100)

    opcoes = {
        "format": "bestaudio/best",              # melhor stream de áudio disponível
        "outtmpl": os.path.join(pasta, "%(title)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "http_chunk_size": 10 * 1024 * 1024,     # evita limitação de velocidade
        "concurrent_fragment_downloads": 4,      # formatos fragmentados em paralelo
        "retries": 5,
        "fragment_retries": 5,
        "progress_hooks": [progresso],
        "writethumbnail": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
             "preferredquality": "320"},          # bitrate máximo do MP3
            {"key": "FFmpegMetadata"},            # título/artista no arquivo
            {"key": "EmbedThumbnail"},            # capa do vídeo no MP3
        ],
    }
    if os.path.isdir(FFMPEG_DIR):
        opcoes["ffmpeg_location"] = FFMPEG_DIR
    return opcoes


def _converter(item: dict) -> None:
    pasta = tempfile.mkdtemp(prefix="ytmp3_")
    try:
        _atualizar(item, status="baixando", progresso=0)
        with yt_dlp.YoutubeDL(_opcoes(pasta, item)) as ydl:
            info = ydl.extract_info(item["url"], download=True)
        mp3s = [f for f in os.listdir(pasta) if f.endswith(".mp3")]
        if not mp3s:
            raise RuntimeError("a conversão não gerou o arquivo MP3")
        titulo = (info or {}).get("title") or os.path.splitext(mp3s[0])[0]
        nome = re.sub(r'[\\/:*?"<>|]+', "", titulo).strip() or "audio"
        destino = _nome_unico(pasta_saida(), f"{nome}.mp3")
        shutil.move(os.path.join(pasta, mp3s[0]), destino)
        _atualizar(item, status="pronto", progresso=100, titulo=titulo,
                   arquivo=destino, nome=os.path.basename(destino))
    except Exception as e:  # noqa: BLE001
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
        _atualizar(item, status="erro", erro=msg)
    finally:
        shutil.rmtree(pasta, ignore_errors=True)


def _expandir_playlist(item: dict) -> None:
    """Transforma um link de playlist em um item por vídeo."""
    try:
        with yt_dlp.YoutubeDL({"extract_flat": "in_playlist", "quiet": True,
                               "no_warnings": True}) as ydl:
            info = ydl.extract_info(item["url"], download=False)
        entradas = [e for e in (info.get("entries") or []) if e]
        if not entradas:
            raise RuntimeError("playlist vazia ou indisponível")
        with _lock:
            _itens.pop(item["id"], None)
        for e in entradas:
            url = e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}"
            if not url.startswith("http"):
                url = f"https://www.youtube.com/watch?v={url}"
            _executor.submit(_converter, _novo_item(url, e.get("title")))
    except Exception as e:  # noqa: BLE001
        _atualizar(item, status="erro", erro=str(e).replace("ERROR: ", ""))


def _eh_playlist(url: str) -> bool:
    return "list=" in url and "watch?v=" not in url and "youtu.be/" not in url


def _publico(item: dict) -> dict:
    return {k: item[k] for k in ("id", "titulo", "status", "progresso", "erro", "nome")}


def _local() -> bool:
    return request.remote_addr in ("127.0.0.1", "::1")


def _ip_permitido(endereco: str | None) -> bool:
    try:
        ip = ipaddress.ip_address((endereco or "").split("%")[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return any(ip in rede for rede in REDES_PERMITIDAS)


# ------------------------------------------------------------- tailscale

_SEM_JANELA = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_cache_ts = {"quando": 0.0, "dados": None}


def _tailscale_cli() -> str | None:
    for c in (shutil.which("tailscale"),
              os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                           "Tailscale", "tailscale.exe")):
        if c and os.path.exists(c):
            return c
    return None


def info_tailscale() -> dict:
    """Endereços deste PC na rede Tailscale (com cache de 20 s)."""
    if time.time() - _cache_ts["quando"] < 20 and _cache_ts["dados"]:
        return _cache_ts["dados"]
    dados = {"instalado": False, "conectado": False, "ip": None, "nome": None}
    cli = _tailscale_cli()
    if cli:
        dados["instalado"] = True
        try:
            saida = subprocess.run([cli, "status", "--json"], capture_output=True, timeout=5,
                                   creationflags=_SEM_JANELA).stdout
            st = json.loads(saida or b"{}")
            eu = st.get("Self") or {}
            ips = [i for i in (eu.get("TailscaleIPs") or []) if "." in i]
            if st.get("BackendState") == "Running" and ips:
                dados.update(conectado=True, ip=ips[0],
                             nome=(eu.get("DNSName") or "").rstrip(".") or None)
        except Exception:  # noqa: BLE001
            pass
    if not dados["ip"]:
        # Sem a linha de comando: procura um endereço 100.64.0.0/10 nas interfaces.
        try:
            for fam, _, _, _, end in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                if ipaddress.ip_address(end[0]) in REDES_PERMITIDAS[2]:
                    dados.update(conectado=True, ip=end[0])
                    break
        except OSError:
            pass
    _cache_ts.update(quando=time.time(), dados=dados)
    return dados


# ------------------------------------------------------- iniciar com o Windows

_CHAVE_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
_NOME_RUN = "YouTube MP3"


def inicia_com_windows() -> bool | None:
    """None = indisponível (não é Windows / não é o .exe)."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _CHAVE_RUN) as k:
            winreg.QueryValueEx(k, _NOME_RUN)
            return True
    except OSError:
        return False


def definir_inicio_windows(ligar: bool) -> None:
    if inicia_com_windows() is None:
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _CHAVE_RUN, 0, winreg.KEY_SET_VALUE) as k:
        if ligar:
            winreg.SetValueEx(k, _NOME_RUN, 0, winreg.REG_SZ,
                              f'"{sys.executable}" --segundo-plano')
        else:
            try:
                winreg.DeleteValue(k, _NOME_RUN)
            except OSError:
                pass


# ------------------------------------------------------------------- rotas

@app.before_request
def so_rede_permitida():
    if not _ip_permitido(request.remote_addr):
        abort(403)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/ping")
def ping():
    with _lock:
        _estado["ultimo_ping"] = time.time()
    return ASSINATURA


@app.post("/adicionar")
def adicionar():
    texto = request.form.get("links") or (request.get_json(silent=True) or {}).get("links", "")
    links = list(dict.fromkeys(LINK_RE.findall(texto)))   # sem repetidos, mantendo a ordem
    if not links:
        return jsonify(erro="Nenhum link do YouTube encontrado."), 400
    for url in links:
        if not url.lower().startswith("http"):
            url = "https://" + url
        if _eh_playlist(url):
            item = _novo_item(url, "Lendo playlist…", status="playlist")
            threading.Thread(target=_expandir_playlist, args=(item,), daemon=True).start()
        else:
            _executor.submit(_converter, _novo_item(url))
    return jsonify(adicionados=len(links))


@app.get("/status")
def status():
    with _lock:
        _estado["ultimo_ping"] = time.time()
        itens = [_publico(i) for i in _itens.values()]
    return jsonify(itens=itens, local=_local(), pasta=pasta_saida(), simultaneos=SIMULTANEOS)


@app.get("/arquivo/<int:item_id>")
def arquivo(item_id: int):
    with _lock:
        item = _itens.get(item_id)
    if not item or not item["arquivo"] or not os.path.exists(item["arquivo"]):
        abort(404)
    return send_file(item["arquivo"], mimetype="audio/mpeg", as_attachment=True,
                     download_name=item["nome"])


@app.get("/zip")
def baixar_zip():
    with _lock:
        prontos = [i for i in _itens.values()
                   if i["status"] == "pronto" and i["arquivo"] and os.path.exists(i["arquivo"])]
    if not prontos:
        abort(404)
    buffer = io.BytesIO()
    # MP3 já é comprimido: ZIP_STORED é instantâneo e o arquivo fica do mesmo tamanho.
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as z:
        for i in prontos:
            z.write(i["arquivo"], i["nome"])
    buffer.seek(0)
    return send_file(buffer, mimetype="application/zip", as_attachment=True,
                     download_name="YouTube MP3.zip")


@app.post("/limpar")
def limpar():
    """Remove da lista os itens concluídos (os arquivos continuam na pasta)."""
    with _lock:
        for k in [k for k, i in _itens.items() if i["status"] in ("pronto", "erro")]:
            del _itens[k]
    return jsonify(ok=True)


@app.get("/acesso")
def acesso():
    ts = info_tailscale()
    url = f"http://{ts['ip']}:{PORTA_PADRAO}" if ts["ip"] else None
    qr = None
    if url:
        import segno
        qr = segno.make_qr(url).svg_inline(scale=5, border=2, dark="#000", light="#fff")
    return jsonify(tailscale=ts, url=url,
                   url_nome=f"http://{ts['nome']}:{PORTA_PADRAO}" if ts["nome"] else None,
                   qr=qr, inicia_com_windows=inicia_com_windows(), local=_local())


@app.post("/inicio-windows")
def inicio_windows():
    if not _local():
        abort(403)
    definir_inicio_windows(bool((request.get_json(silent=True) or {}).get("ligar")))
    return jsonify(inicia_com_windows=inicia_com_windows())


@app.post("/abrir-pasta")
def abrir_pasta():
    if not _local():
        abort(403)
    pasta = pasta_saida()
    if sys.platform == "win32":
        os.startfile(pasta)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", pasta])
    else:
        subprocess.Popen(["xdg-open", pasta])
    return jsonify(ok=True)


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
        "--window-size=720,760", "--no-first-run", "--no-default-browser-check",
        "--disable-background-mode", "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
    ])


def _ocupado() -> bool:
    with _lock:
        return any(i["status"] in ATIVOS for i in _itens.values())


def vigiar(processo: subprocess.Popen | None) -> None:
    """Sem bandeja (Linux/macOS): encerra quando a janela é fechada e nada converte."""
    inicio = time.time()
    while True:
        time.sleep(2)
        if _ocupado():
            continue
        with _lock:
            ultimo = _estado["ultimo_ping"]
        referencia = ultimo or inicio
        if time.time() - referencia > (150 if ultimo else 120):
            os._exit(0)
        if processo is not None and processo.poll() is not None and ultimo \
                and time.time() - ultimo > 10:
            os._exit(0)


def rodar_bandeja(endereco: str) -> None:
    """Ícone ao lado do relógio do Windows: Abrir / Iniciar com o Windows / Sair."""
    import pystray
    from PIL import Image

    def sair(icone, _item=None):
        if _ocupado():
            icone.notify("Ainda há conversões em andamento; elas serão interrompidas.",
                         "YouTube MP3")
            time.sleep(2)
        icone.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Abrir YouTube MP3", lambda: abrir_janela(endereco), default=True),
        pystray.MenuItem("Abrir pasta dos MP3", lambda: os.startfile(pasta_saida())),  # noqa: S606
        pystray.MenuItem("Iniciar com o Windows",
                         lambda _i, item: definir_inicio_windows(not item.checked),
                         checked=lambda _item: bool(inicia_com_windows())),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Sair", sair),
    )
    imagem = Image.open(os.path.join(BASE, "static", "icone.png"))
    pystray.Icon("youtube-mp3", imagem, "YouTube MP3", menu).run()


def main() -> None:
    segundo_plano = "--segundo-plano" in sys.argv
    if "--servidor" in sys.argv:  # modo servidor puro, sem janela nem bandeja
        app.run(host="0.0.0.0", port=PORTA_PADRAO, threaded=True)
        return

    endereco = f"http://127.0.0.1:{PORTA_PADRAO}"
    if ja_esta_rodando(PORTA_PADRAO):        # já aberto: só mostra a janela
        if not segundo_plano:
            abrir_janela(endereco)
        return

    # 0.0.0.0 para o Tailscale alcançar; o filtro de IP barra o resto da rede.
    servidor = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=PORTA_PADRAO, threaded=True), daemon=True)
    servidor.start()
    for _ in range(50):  # espera o servidor responder
        if ja_esta_rodando(PORTA_PADRAO):
            break
        time.sleep(0.1)

    processo = None if segundo_plano else abrir_janela(endereco)
    if sys.platform == "win32":
        try:
            rodar_bandeja(endereco)
            return
        except Exception as e:  # noqa: BLE001
            print("Bandeja indisponível:", e, file=sys.stderr)
        if segundo_plano:          # sem bandeja e sem janela: só atende o celular
            threading.Event().wait()
    vigiar(processo)


if __name__ == "__main__":
    main()
