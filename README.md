# YouTube → MP3

Cole um link do YouTube e baixe o áudio em **MP3 320 kbps** (com título e capa embutidos).

## Windows: só clicar
1. Copie `dist/YouTube-MP3.exe` para onde quiser (ex.: Documentos).
2. Dê dois cliques. Abre uma janela do aplicativo.
3. Cole um ou vários links (um por linha; playlists também). Eles são convertidos vários ao mesmo tempo e salvos sozinhos em **Downloads\YouTube MP3**.
4. Para ter atalho: clique com o botão direito no ícone da barra de tarefas → **Fixar na barra de tarefas**.

Na primeira vez o Windows pode avisar "O Windows protegeu o computador" (o programa não tem assinatura digital): clique em **Mais informações → Executar assim mesmo**.
Não precisa instalar Python nem ffmpeg.

## Usar no iPhone de qualquer lugar (Tailscale)
1. Instale o **Tailscale** no PC e no iPhone (tailscale.com/download) e entre com a **mesma conta** nos dois.
2. No PC, abra o YouTube MP3 e clique em **Usar no iPhone (Tailscale)**: aparece um QR code.
3. No iPhone, aponte a câmera para o QR code. No Safari, toque em **Compartilhar → Adicionar à Tela de Início**.
4. Pronto: o ícone na tela do iPhone abre o app no Safari. Os MP3 vão para o app **Arquivos**.

O PC precisa estar ligado com o app rodando. Fechar a janela **não** desliga o app: ele fica no ícone ao lado do relógio
(menu: Abrir / Abrir pasta / Iniciar com o Windows / Sair). Marque **Iniciar com o Windows** para ele estar sempre pronto.
Só o próprio PC e os aparelhos da sua conta Tailscale conseguem acessar; o resto da rede é bloqueado.

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
