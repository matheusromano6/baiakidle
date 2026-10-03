# Como publicar mudanças do bot e do market (releases)

Documento de referência das DUAS conversas que trabalham neste produto: a do **Bot**
(gui.py, bot.py, rotinas) e a do **Market Baiakidle** (pasta `market/`). É o mesmo
produto, o mesmo repositório (`matheusromano6/baiakidle`), o mesmo contador de
versões e a mesma pasta de trabalho. Leia inteiro antes de mexer em build/release.

O `market/` viaja DENTRO do bot. As pessoas só recebem uma mudança (do market ou do
bot) quando sai uma RELEASE nova no GitHub: o botão "Atualizar" compara a última
release com `bot.VERSION`.

**Regra de ouro:** toda mudança que precise chegar nos outros PCs/Macs = bump de versão +
build + release NOVA. Nunca reaproveite uma tag/versão (quem já está nela acharia que
está atualizado). Só faça commit/push/release quando o usuário pedir.

---

## 0. Antes de começar
- `git status` e `git log --oneline -5`; `gh release list -L 3` (neste PC o `gh` só
  funciona no PowerShell, não no Bash).
- A próxima versão tem que ser MAIOR que a última tag (correção = 4.15.1, feature =
  4.16.0).
- A pasta de trabalho é compartilhada com a outra conversa: não reverta nem commite o
  que não é seu; veja o `git diff` de cada arquivo antes de dar `git add`.

## 1. Arquivos pessoais NUNCA vão no build
`market.db*`, `backups/`, `chrome_profile/`, `config.json`, `codex_progress.json*`,
`__pycache__`, handoff pdf, `*.state.json`, `settings*.json`, `routines*.json`.

Se criar um arquivo novo de DADOS/pessoal no `market/`, adicione nas TRÊS listas (mantenha
sincronizadas):
1. `.gitignore`
2. `make_share_zip.py` -> `MARKET_EXCLUDE_NAMES`
3. `.github/workflows/build-macos.yml` -> `--exclude` do rsync no passo "Copiar o codigo do market/"

Arquivo novo de CÓDIGO (ex: `market/codex.py`) é só `git add` - vai sozinho no build.
Atenção: arquivo novo aparece como `??` no `git status`; `git add` só dos modificados
deixa ele de fora.

## 2. Dependências novas
O market é carregado em runtime, então o PyInstaller NÃO enxerga os imports dele (foi
a causa do "No module named 'sqlite3'"). Se o market (ou o bot) passar a importar um
módulo novo (stdlib ou pacote), adicione `--hidden-import <modulo>` em `build.bat` E no
`build-macos.yml`; pacote de terceiros também entra em `requirements.txt`.

## 3. Verificar
`python -m py_compile` nos arquivos mexidos + teste funcional. Não gastar nem consumir
nada da conta do jogo nos testes sem autorização do usuário.

## 4. Bump + commit + push
- Versão: `bot.py`, linha 18 `VERSION = "X.Y.Z"`. Edite com a ferramenta de edição ou
  `sed`. NUNCA use PowerShell `Get-Content`/`Set-Content` em `.py` (corrompe a
  codificação UTF-8).
- `git add` dos arquivos EXPLÍCITOS, incluindo os novos não rastreados. Sem `git add -A`.
  Não commite `market/items.json` nem settings/routines se foram só dado de runtime
  (confira o diff). Mensagem em português + trailer `Co-Authored-By`.
- `git push origin main` (isso já dispara o build do Mac no Actions).

## 5. Build Windows (PowerShell, na pasta `bot_baiak_idle`)
Se o usuário estiver com o bot rodando (processo `BaiakIdleBot`), o
`dist\BaiakIdleBot.exe` fica travado - NÃO mate o processo; builde numa pasta separada.
(`build.bat` termina com `pause`, então prefira o comando abaixo.)

```powershell
$driver = python -c "import playwright, os; print(os.path.join(os.path.dirname(playwright.__file__), 'driver'))"
$icon = (Resolve-Path "icon.ico").Path
python -m PyInstaller --onefile --windowed --name BaiakIdleBot --icon $icon --add-data "$driver;playwright\driver" --hidden-import sqlite3 --distpath dist_fix --workpath build_fix --specpath spec_fix gui.py --noconfirm
```
(inclua os `--hidden-import` novos do passo 2).

## 6. Zip do Windows (nome EXATO: `BaiakIdleBot-windows.zip`)
Mesmo conteúdo do `make_share_zip.py` (exe + icon + routines/settings PADRÃO + `market/`
sem os arquivos pessoais). Com `dist_fix`:

```
python -c "
import json, shutil, zipfile, os, bot
EX=('market.db','market.db-shm','market.db-wal','backups','chrome_profile','config.json','__pycache__','handoff-baiak-market-alert.pdf','codex_progress.json','codex_progress.json.tmp')
t='dist_fix/_pkg'; os.makedirs(t)
shutil.copy('dist_fix/BaiakIdleBot.exe', t); shutil.copy('icon.ico', t)
json.dump(bot.DEFAULT_ROUTINES, open(t+'/routines.json','w',encoding='utf-8'), ensure_ascii=False, indent=2)
json.dump(bot.DEFAULT_SETTINGS, open(t+'/settings.json','w',encoding='utf-8'), ensure_ascii=False, indent=2)
shutil.copytree('market', t+'/market', ignore=shutil.ignore_patterns(*EX))
with zipfile.ZipFile('dist_fix/BaiakIdleBot-windows.zip','w',zipfile.ZIP_DEFLATED) as z:
    for r,_,fs in os.walk(t):
        for n in fs: p=os.path.join(r,n); z.write(p, os.path.relpath(p,t))
shutil.rmtree(t)"
```
(a lista `EX` tem que ser igual à `MARKET_EXCLUDE_NAMES` do `make_share_zip.py`.)

## 7. Release (PowerShell). Notas num ARQUIVO (aspas no texto quebram o comando)
```powershell
gh release create vX.Y.Z "dist_fix\BaiakIdleBot-windows.zip" --title "vX.Y.Z" --notes-file notas.md
```

## 8. Asset do Mac (nome EXATO: `BaiakIdleBot-macos-arm64.zip`)
1. Espere o build do push: `gh run list --workflow=build-macos.yml --limit 1` = success.
2. NÃO use `gh run download` (trava). Baixe pela API (~110MB, leva minutos - rode em
   background):
```powershell
$id = (gh api repos/matheusromano6/baiakidle/actions/runs/<RUN_ID>/artifacts | ConvertFrom-Json).artifacts[0].id
Invoke-WebRequest -Uri "https://api.github.com/repos/matheusromano6/baiakidle/actions/artifacts/$id/zip" -Headers @{Authorization="Bearer $(gh auth token)"} -OutFile dist_fix\mac_artifact.zip
```
3. Reempacote COPIANDO os `ZipInfo` (preserva o bit de execução do app). NUNCA use
   `Expand-Archive`/`Compress-Archive`: perdem o +x e o `.app` deixa de abrir:
```
python -c "
import zipfile
s=zipfile.ZipFile('dist_fix/mac_artifact.zip'); d=zipfile.ZipFile('dist_fix/BaiakIdleBot-macos-arm64.zip','w',zipfile.ZIP_DEFLATED)
for i in s.infolist(): d.writestr(i, s.read(i.filename))
d.close()"
```
   Confira: `BaiakIdleBot.app/Contents/MacOS/BaiakIdleBot` tem de estar `0o100755`.
4. `gh release upload vX.Y.Z dist_fix\BaiakIdleBot-macos-arm64.zip --clobber`

## 9. Fechar
`gh release view vX.Y.Z` deve listar os DOIS assets. Apague `dist_fix/`, `build_fix/`,
`spec_fix/`. `git status` só pode sobrar o que não é seu.

## Avisos
- Windows: o market roda da pasta `market/` ao lado do `.exe`; o botão "Atualizar" faz
  MERGE do código por cima (dados pessoais ficam). Mac: o código vem embutido no `.app`
  e os dados ficam em `~/Library/Application Support/BaiakIdleBot`.
- Quem está na 4.13.1 ou anterior precisa atualizar manualmente uma vez (baixar o zip
  da release, trocar só o `.exe` e a pasta `market/`) - o atualizador antigo tem um bug.
- Sem force push; não apague release/tag já publicada.

---

# Alinhamento entre as conversas (Bot <-> Market)

As duas conversas mexem no mesmo produto e na mesma pasta. O usuário é a ponte entre
elas: **o que uma muda e a outra precisa saber vai escrito na nota de handoff** (abaixo).

## Quem é dono de quê
| Área | Dona |
|---|---|
| `market/*` (código, dashboard, scanner, api, store) | Market |
| Leitura do Codex para o market (`read_codex_progress` e afins no `bot.py`) | Market (mas usa helpers do Bot - ver contratos) |
| `gui.py`, rotinas, Campanha de Codex, Poções, atualizador | Bot |
| `build.bat`, `build-macos.yml`, `make_share_zip.py`, `.gitignore`, este arquivo | Compartilhado: mudou, avise a outra |

## Contratos que as duas precisam respeitar
1. **Helpers compartilhados do `bot.py`** (`open_codex`, `codex_hunts_view`,
   `read_codex_hunt_entries`, `click_codex_pager_button`, `build_codex_entry`,
   `request_from_bot`, `load_state`/`save_state`): não mude o comportamento sem checar quem
   usa (Campanha de Codex, Poções e leitura do market). Prefira ADICIONAR função nova a
   alterar uma existente.
2. **Filtros do Codex ficam LIGADOS** ("Entregáveis", "Esconder concluídos", "Esconder
   bloqueadas"): o botão "Desbloquear" tem a mesma classe (`.cx-give`) do "Entregar",
   e a rotina de entrega clica nesse seletor. Quem desligar os filtros para ler tem que
   devolver (use `codex_hunts_view`, que restaura sozinho).
3. **Dados que o bot grava enquanto roda** vão em `*.state.json` (`load_state`/`save_state`),
   NÃO em `settings.json`: a GUI regrava o `settings.json` inteiro e apagaria o que o bot
   gravou ali.
4. **Ler/mexer no jogo a partir da interface** com o bot rodando: passe pela thread do bot
   (`request_from_bot`); uma segunda conexão ao jogo briga com a rotina de entrega.
5. **Gold/itens da conta só com autorização explícita do usuário** (compra, desbloqueio,
   uso de poção). Os caminhos que gastam têm travas (custo autorizado, conferência do
   carrinho/confirmação); não as enfraqueça.
6. **Um contador de versão só** (`bot.VERSION`). Antes de bumpar, confira
   `gh release list` - a outra conversa pode ter publicado.
7. **Fatos do jogo já confirmados** (não descubra de novo): bônus de cada nível do Codex é
   aditivo; o Codex pagina de 30 em 30 e o título do botão "próxima" some a partir da pág. 2
   (clicar por posição); Mercador vende 6 poções a 10kk com limite de 1 por tipo por dia;
   cada uso de poção soma +30 min (empilha).

## Nota de handoff (colar no fim de cada release/mudança relevante e levar à outra conversa)
```
HANDOFF <Bot|Market> -> <Market|Bot> - vX.Y.Z
- O que mudou em áreas compartilhadas (bot.py/gui.py/build/ignore/listas de exclusão):
- Funções/contratos novos ou alterados (nome + o que mudou):
- Arquivo novo de dados/pessoal? (já nas 3 listas?):
- Dependência nova / --hidden-import:
- O que a outra conversa precisa fazer ou saber:
```
