# YouTube → MP3

Cole um link do YouTube e baixe o áudio em **MP3 320 kbps** (com título e capa embutidos).

## Windows: só clicar
1. Copie `dist/YouTube-MP3.exe` para onde quiser (ex.: Documentos).
2. Dê dois cliques. Abre uma janela do aplicativo.
3. Cole um ou vários links (um por linha; playlists também). Eles são convertidos vários ao mesmo tempo e salvos sozinhos em **Downloads\YouTube MP3**.
4. Para ter atalho: clique com o botão direito no ícone da barra de tarefas → **Fixar na barra de tarefas**.

Na primeira vez o Windows pode avisar "O Windows protegeu o computador" (o programa não tem assinatura digital): clique em **Mais informações → Executar assim mesmo**.
Fechar a janela encerra o programa. Não precisa instalar Python nem ffmpeg.

## Rodar a partir do código (desenvolvedores)

### Requisitos
- Python 3.10+
- ffmpeg instalado (`sudo apt install ffmpeg`, `brew install ffmpeg` ou `winget install ffmpeg`)

### Como rodar
```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```
O app abre sozinho em http://127.0.0.1:8765

### Dica
O YouTube muda com frequência. Se a conversão começar a falhar, atualize o yt-dlp:
```bash
pip install -U yt-dlp
```

Use apenas com conteúdo que você tem direito de baixar.
