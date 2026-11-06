# Guia — Como gerar e incluir as imagens de evidência no repositório

Este guia mostra, passo a passo, como produzir as quatro imagens que o
`README.md` já referencia e como colocá-las no GitHub.

| # | Arquivo (nome exato) | O que mostra |
|---|---|---|
| 1 | `docs/img/circuito_bancada.jpg` | Foto do circuito montado na bancada |
| 2 | `docs/img/circuito_funcionando.gif` | GIF curto: apertar os botões e ver a senha no display |
| 3 | `docs/img/evidencia_envio_repl.png` | Print do Shell/REPL com os pacotes enviados e a fila esvaziando |
| 4 | `docs/img/evidencia_servidor.png` | Print do terminal do servidor com as linhas `[201]` |

> ⚠️ **Os nomes precisam ser exatamente estes, em minúsculas.** O Windows não
> diferencia `Circuito.JPG` de `circuito_bancada.jpg`, mas o GitHub diferencia —
> com outro nome a imagem aparece quebrada no README.

---

## Passo 1 — Deixar tudo funcionando

Siga as Etapas 1 a 8 do [passo a passo](passo_a_passo.md): circuito ligado,
servidor rodando, totem emitindo senhas e o servidor mostrando `[201]`. As
fotos e o GIF devem mostrar o circuito "vivo" (display aceso com uma senha,
cabo USB ligado).

## Passo 2 — Foto do circuito (`circuito_bancada.jpg`)

1. Fundo limpo (a bancada ou uma folha branca), boa luz, **sem flash** (o flash reflete no display e esconde a senha).
2. Aperte um botão antes, para o display mostrar uma senha (ex.: `N003`).
3. Celular **paralelo à bancada**, de cima, enquadrando a placa inteira, a protoboard com os 2 botões e o buzzer, e os fios até os pinos.
4. Tire 3 ou 4 fotos e escolha a mais nítida — os fios e os números dos pinos devem ser legíveis.
5. Passe a foto para o computador (cabo, Google Fotos, ou WhatsApp Web enviando como **Documento** para não perder qualidade).
6. **Reduza o tamanho** para ~1600 px de largura: no Windows, abra no app *Fotos* → `…` → *Redimensionar imagem*; ou use [squoosh.app](https://squoosh.app). Meta: **menos de 1 MB**.
7. **Remova a localização (GPS)** antes de publicar: botão direito no arquivo → *Propriedades* → *Detalhes* → *Remover Propriedades e Informações Pessoais* → *Criar uma cópia…*.
8. Renomeie para `circuito_bancada.jpg` e salve em `docs/img/`.

## Passo 3 — GIF do totem funcionando (`circuito_funcionando.gif`)

1. Com o celular fixo (apoie em um copo ou livro), grave **8 a 10 segundos**:
   - toque no botão azul duas vezes (display: `N001`, `N002`);
   - toque no botão branco (display: `P001`);
   - segure um botão 2 s para mostrar o cancelamento (`Cancelada P001`).
2. Converta o vídeo em GIF com **uma** das opções:
   - **Sem instalar nada:** [ezgif.com/video-to-gif](https://ezgif.com/video-to-gif) → envie o vídeo → *Size*: 640, *Frame rate*: 10 → *Convert* → *Optimize* se passar de 5 MB → *Save*.
   - **Com ffmpeg** (`winget install ffmpeg`):
     ```bash
     ffmpeg -i video.mp4 -t 10 -vf "fps=10,scale=640:-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse" circuito_funcionando.gif
     ```
3. Meta: **até 5 MB**. Salve como `docs/img/circuito_funcionando.gif`.

## Passo 4 — Prints do envio dos pacotes JSON

1. **Shell do Thonny (ou terminal do MicroPico):** faça a demonstração da fila (Etapa 8.1 do passo a passo) — desligue o servidor, aperte os botões, ligue de novo. O print ideal mostra as linhas `[http] Falha de rede`, depois `[http] 3 pacote(s) enviados | pendentes na fila: 0`.
2. Pressione **Win + Shift + S**, selecione só a área do Shell e, no aviso que aparece, clique em *Salvar como* → `docs/img/evidencia_envio_repl.png`.
3. Repita no terminal do servidor (linhas `[201]`; os reenvios chegam juntos, com o `gerado=` antigo) → `docs/img/evidencia_servidor.png`.
4. Confira se nenhum print mostra a senha do Wi-Fi (o `config.py` aberto no editor, por exemplo).

## Passo 5 — Enviar para o GitHub

**Opção A — terminal (na pasta do repositório):**
```bash
git add docs/img/
git status                      # confira: config.py NÃO pode aparecer na lista
git commit -m "docs: fotos, GIF e evidências do envio"
git push
```

**Opção B — VS Code:** aba *Source Control* (`Ctrl+Shift+G`) → `+` nos arquivos de `docs/img/` → mensagem → *Commit* → *Sync Changes*.

**Opção C — site do GitHub:** abra a pasta `docs/img` no repositório → *Add file* → *Upload files* → arraste as imagens → *Commit changes*.

## Passo 6 — Conferir

Abra o repositório no GitHub e role até a seção **Evidências** do README. As
quatro imagens devem aparecer. Se alguma estiver quebrada, confira o nome
(maiúsculas/minúsculas e extensão `.jpg` vs `.jpeg`) e se o arquivo está mesmo
em `docs/img/`.
