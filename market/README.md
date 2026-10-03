# Baiak Idle - alerta de oportunidades no market

Sistema **somente leitura** que monitora o leilão (market) do Baiak Idle,
constrói uma base histórica de preços, avisa quando um leilão ativo está bem
abaixo do valor histórico do item, e acompanha suas compras/vendas e o lucro.

Não automatiza lance/compra/venda. Você age manualmente no jogo/site.

## Painel web (recomendado)

```bash
python server.py
```

Abre `http://127.0.0.1:8787` no navegador. Deixe rodando com a aba aberta.

- **Navbar fixa** no topo com atalho pra cada seção (clicou, rola até ela e já
  expande se estiver recolhida), contadores (oportunidades por tipo,
  posições, ofertas boas nas buscas) e a seção atual em destaque. Ao rolar a
  página aparece o botão **↑** no canto inferior direito pra voltar ao topo.
  **Posições abertas**, **Vendidas e encerradas** e **Histórico de análises** nascem
  **recolhidas** (o título mostra a contagem; clique pra expandir/recolher).
  Posições abertas abre sozinha quando há posição de verdade e **nunca esconde
  um lance coberto ou leilão encerrado** - nesse caso abre à força e o atalho
  "Posições" pisca em vermelho. O que você abre/fecha vale até recarregar a
  página.
- **Oportunidades**: separadas em **Equipamento**, **Lote (empilháveis)** e
  **Gold**, porque as colúnas relevantes diferem. Cada linha traz um rótulo
  de qualidade - **boa / muito boa / excelente** (cor na linha) - além de
  preço atual, lance máximo sugerido, desconto vs mediana, **economia
  estimada em coins** (se revendido à mediana, antes do rake), liquidez e
  countdown. Botão **Dar lance** (registra a intenção com seu lance máximo)
  ou **Ignorar**. Leilões que já encerraram somem da lista.
- **Coluna "No mercado"** (pressão de venda): leilões duram só **~6h**, então
  quase tudo que está listado fecha logo. A coluna mostra quantos anúncios
  iguais estão ativos e, embaixo, **quantas vezes há mais vendedores que
  compradores** na janela em que eles fecham (`flip_horizon_hours`, 8h):
  `pressão = anúncios fechando em 8h ÷ (vendas/dia × 8/24)`.
  Verde ≤ 1x, âmbar 1-2x, vermelho > 2x. `> oversupply_pressure` (3,5) faz a
  qualidade **cair um nível**.
- **Tendência de preço (coluna Qualidade, terceira linha)**: compara a
  **mediana dos últimos 2 dias** com a do resto da semana e mostra sempre um
  selo - **📉 -N%** (caindo), **➡ estável**, **📈 +N%** (subindo) ou **tend.
  s/ dado** (menos de `trend_min_recent` vendas recentes). O `preço justo` usa
  o **menor** entre a mediana da semana e a recente (não persegue alta, protege
  na queda), então um item que caiu de 100 pra 55 não aparece mais como "45%
  de desconto" falso. `caindo ≤ trend_down` (-10%) **derruba a qualidade um
  nível** e força o veredito pra "só usar" - preço em queda + muitos no mercado
  é a armadilha clássica: você compra "barato" mas não revende. No backtest,
  "parece barato" com preço caindo >15% acertou só 33% das vezes (vs 62%
  quando estável).
- **Veredito (na coluna Qualidade)**: sintetiza tudo numa recomendação:
  - **💰 revender** - pressão ≤ `flip_max_pressure` (2), `vendas/dia ≥
    flip_min_liquidity` (1) **e** preço não está caindo. A demanda absorve:
    comprar barato e revender vale.
  - **🛡 só usar** - muitos anúncios fechando junto, pouca liquidez **ou preço
    caindo**: revender derrubaria o preço / a queda continua. Só compensa se
    for equipar. Dentro de cada qualidade, os "revender" vêm primeiro.
- **Filtro por personagem**: barra "Ver: Mercado todo / Meus personagens".
  Cadastre personagens (vocação + level). No modo "Meus personagens" a lista
  mostra só **equipamentos** que algum personagem **ativo** consegue usar
  (vocação compatível e level suficiente); gold e itens de trade somem.
  Clique no chip do personagem pra ativar/desativar. A tabela de itens do
  jogo (nome → slot/level/vocação) é extraída do site e cacheada em
  `items.json` (atualiza a cada `items_refresh_days`).
- **Copiar nome**: o ⧉ ao lado de cada item copia o nome pro clipboard, pra
  colar na busca do market dentro do jogo.
- **▸ mais detalhes**: abaixo dos atributos, um link recolhível mostra o
  resto da ficha do item - ataque, defesa, armadura, dano/tipo elemental,
  alcance, skill boost, absorção por elemento e quantos slots de imbuement o
  item aceita (e quais categorias). Fica escondido pra não poluir a linha;
  clique expande. Vem do mesmo extrator de `items.py` (bundle do jogo) que já
  dava vocação/level/classe. Aparece em Oportunidades, Procurar por oferta,
  Histórico de análises.
- **Miniatura do item** ao lado do nome + **tooltip ao passar o mouse**
  (equipamento): mostra o ícone de verdade do jogo (mesma API que o próprio
  market oficial usa, `api/things/object/<id>.png` - cobertura completa,
  todo item tem) e, no hover, um card no estilo do jogo - raridade, classe,
  ícone grande, tipo/combate/nível/vocações e os bônus (skill boost,
  resistência elemental colorida, crítico, leech, velocidade) além dos
  atributos, um por linha. Não tem o campo "Valor" (preço de NPC) porque
  essa informação não está disponível na API/bundle público do site.
  Vale pra **toda tabela que mostra equipamento**: Oportunidades, Procurar
  por oferta, Histórico de análises, Posições abertas,
  Vendidas e encerradas, Acompanhando (lista e busca) e Consultar preço de
  venda.
- **Posições abertas**: acompanha o leilão de cada item que você deu lance -
  preço atual, se ainda está dentro do seu lance ou foi superado, countdown.
  Quando o leilão encerra, você confirma **Arrematei** (informa o preço pago)
  ou **Perdi**.
    A coluna **"Revender / Usar até"** guarda as referências de quando você deu
    o lance, pra você saber até onde subir. Ficam vermelhas quando o leilão
    passa delas.
  - **Editar lance**: enquanto o leilão está ativo, dá pra atualizar seu lance
    máximo a qualquer momento ("Editar lance" / "Aumentar lance"). O prompt
    mostra o preço atual, seu máximo, e os tetos revender/usar; avisa se você
    for pagar acima do preço justo.
  - **Alerta de lance coberto**: quando o preço passa do seu lance máximo (ou
    o leilão encerra sem confirmação), a linha **pisca em vermelho**, a página
    **rola até ela**, o **título da aba pisca** e - se você clicou no 🔔 -
    chega um **aviso no desktop**.
- **Acompanhando**: pra seguir ao vivo um leilão específico que ainda vai
  demorar (fora da janela de oportunidades). Busca pelo nome → lista os
  leilões ativos daquele item com o **#id** → clica **acompanhar** (ou cola
  o id direto). A tabela mostra preço/lances/countdown ao vivo (atualiza a
  cada `track_poll_seconds`) e o **preço justo / revender até**, recalculado a
  cada ciclo (não só quando você adiciona - mais venda no histórico pode
  destravar uma estimativa que faltava). Se o item **não tem histórico exato
  nem da cesta** (raro, ou pouca venda ainda), cai numa **estimativa por tipo**
  (mesmo tipo + raridade, igual ao "Procurar por oferta" faz com achados sem
  histórico - `analyze.value_for_watch`), marcada com **≈** e "estimativa por
  tipo · nN"; sem nenhum dado, mostra "sem histórico ainda" em vez de inventar
  número. Coluna **Retorno estimado** = preço justo × 0,90 (rake) − preço
  agora, pra já ter noção de investimento e retorno sem abrir a conta na mão.
  Quando faltar ~5 min (`watch_alert_seconds`), a linha **pisca**, a página
  rola até ela, o título da aba pisca e chega aviso no desktop.
- **Procurar por oferta**: buscas salvas, agora com **filtros combináveis** (os
  mesmos do market, mais atributos): **nome** (opcional), **tipo** (arma 1 mão,
  arma 2 mãos, elmo, armadura, perneira, botas, escudo, amuleto, anel,
  trinket), **vocação** (MS/ED/EK/RP/MK), **raridade**, **classe** e
  **atributos** (o anúncio precisa ter todos os escolhidos). Ex.: *perneira +
  MS + Exp* - o nome pode ficar vazio. Item sem restrição de vocação entra em
  qualquer vocação. A cada varredura o sistema lista **tudo que está à venda**
  que bate e avalia cada anúncio com a mesma lógica das oportunidades: coluna
  **Usar até** (preço justo daquele anúncio, já com os atributos dele) e
  **Revender até** (com margem; "empata" = onde o rake zera), veredito
  revender/usar e selo boa/muito boa/excelente. Cada anúncio tem os botões
  **Dar lance** (mesmo fluxo de Oportunidades - cria a posição e ela aparece
  em **Posições abertas**, que se abre sozinha) e **acompanhar** (manda pra
  Acompanhando). Como um achado do "Procurar por oferta" não passa pela
  tabela de oportunidades, dar lance nele busca o leilão ao vivo e avalia na
  hora (`analyze.value_for_watch`, mesmo mecanismo do Acompanhando) -
  corrigido junto: antes só dava pra dar lance em algo que já tinha aparecido
  em Oportunidades. Uma vez que você clica **acompanhar**, o botão vira
  **"✓ acompanhando"** (desabilitado) e fica assim enquanto o item estiver na
  lista de Acompanhando - não some, mas também não deixa clicar de novo. **Quando
  uma busca salva encontra uma oferta boa nova** (que não estava boa no ciclo
  anterior), a página pisca o título da aba, manda notificação no desktop (se
  você permitiu) e rola até o card da busca - sem forçar abrir o collapse, que
  continua do jeito que você deixou. O atalho "Procurar" no navbar também fica
  vermelho piscando enquanto houver oferta boa não vista, igual ao de Posições.
  Pra referência rápida ao rolar a tabela, cada anúncio **bom** ganha uma
  borda colorida à esquerda: **azul** = achou (mais de 10 min pra fechar),
  **laranja** = faltam 10 min, **vermelha** (piscando) = falta 1 min ou menos -
  a mesma cor de "precisa agir" usada em Posições abertas. Anúncios que não
  passaram no filtro de qualidade não ganham borda. Anúncio **sem histórico do
  próprio item** mostra uma **estimativa por tipo** (mediana do mesmo tipo +
  raridade, de preferência com os mesmos atributos; marcada com ≈, sem cor nem
  veredito - é referência grosseira). Cada busca é um **bloco recolhível**
  (cabeçalho: `N à venda` + selo **💰 N boa(s)**, ou **"buscando
  oportunidade"**), lista **agrupada por raridade** e colorida, mostrando os 40
  melhores (bons primeiro, depois os que encerram antes). Botão **acompanhar**
  por linha; **parar** remove; pode pausar. Uma **oferta boa nova** gera alerta
  (no máximo 5 por varredura, uma vez por leilão).
- **Estoque**: o que você já comprou (arrematou) e ainda não vendeu nem
  usou - antes ficava misturado em "Posições abertas", agora **Posições
  abertas** mostra só leilões em andamento. Pra cada item: quanto pagou, **vale
  hoje** (preço justo atual, conservador), **sobra se vender** (já descontando
  o rake de 10% e a taxa de listagem), o **resultado não realizado**, o
  **veredito** (vender logo - preço caindo / bom momento / ok / segurar -
  mercado cheio / no piso), o **preço inicial sugerido** e a **melhor hora de
  anunciar** (`sell_list_hours`, padrão 14h-17h: o leilão dura 6h+, então
  anunciar à tarde fecha 20h-23h, quando o mercado está mais quente). Cards no
  topo somam custo, valor, o que sobra e o resultado. **Vendi** registra a
  venda (entra no lucro); **Usei** tira do estoque sem venda (estado `used`,
  aparece em "Vendidas e encerradas" e dá pra reabrir). Item sem histórico
  suficiente mostra "sem base". Gold em estoque é só pra usar.
- **Vendidas e encerradas**: itens arrematados marcados como **Vendido**
  (informa o preço de venda) viram lucro líquido = `venda×(1-10%) - compra -
  taxa de listagem`.
- **Consultar preço de venda**: pra quando você tem um item pra listar.
  Busca pelo nome e mostra, por variante (raridade / +n / forja):
  - **Vender agora?** - o veredito: **✅ bom momento** (poucos concorrentes),
    **vender logo - preço caindo** (a mediana recente despencou, não segure),
    **➖ ok**, **⏳ segurar - mercado cheio** (vai competir e vender barato),
    **🐌 pouca procura**, ou **≈25 no piso** (já no mínimo, tanto faz).
  - quantas venderam (liquidez), preço de venda (mediana · p25-p75 · min-máx),
    nº típico de lances, **quantos estão em aberto agora** + pressão,
    **preço inicial sugerido** e **quanto você recebe** (mediana - rake - taxa).
- **Lucro**: capital inicial (editável), lucro hoje / 7d / 30d / total, ROI,
  capital em aberto, e um seletor de período (data X a data Y).
- **Histórico de análises**: toda oportunidade que o sistema sinalizou fica
  registrada com o preço de quando começou a monitorar. Quando o leilão
  fecha, grava o **preço final** e classifica:
  - **fechou p/ revenda** - fechou ≤ "revender até", com pelo menos 2 lances
  - **fechou no piso** - fechou em 25 (preço mínimo) com 1 lance só: alguém
    pegou no piso, **não conta como preço de mercado** nem como flip perdido
  - **só p/ uso** - entre "revender até" e o preço justo
  - **fechou caro** - acima do preço justo (comprar teria dado prejuízo → a
    coluna mostra o valor negativo de propósito)
  - **expirou sem venda**

  A coluna **"lucro possível"** = `preço justo (do lote) × 0,90 - (fechamento
  + 1)` - o +1 porque pra arrematar você paga pelo menos 1 acima de quem
  ganhou. O resumo traz **"lucro que passou"** (soma dos *fechou p/ revenda*
  que você não pegou), **"investimento necessário"** (quanto teria gasto pra
  pegar todos eles) e o **ROI potencial**. Mostra só as **últimas 24h** e só as
  **excelentes** (💎 Lendário+ com margem grande na última hora).

As tabelas que crescem (posições abertas, vendidas/encerradas, histórico de
análises) são **paginadas** (15-30/página, ‹ ›) e trazem **data** em cada
linha.

### Frequência

- **Novas oportunidades**: varredura completa (`auction.browse`) a cada
  `poll_seconds` (padrão **2 min**).
- **Preços do que já monitora**: cada oportunidade detectada e cada posição
  aberta é consultada individualmente (`auction.item`, 1 request cada) a cada
  `track_poll_seconds` (padrão **10 s**) - mantém desconto/qualidade/countdown
  quase em tempo real. Oportunidade que encerrou ou passou do preço justo sai
  da lista. Falha de rede num item não o remove (tenta de novo no ciclo seguinte).

## CLI (sem painel)

```bash
python run.py collect   # uma passada de coleta (histórico + ativos)
python run.py scan       # coleta e mostra/notifica oportunidades agora
python run.py watch      # loop: coleta + notificação a cada poll_seconds
python run.py stats --name "tainted heart"   # estatísticas de um item
```

## Como funciona

O market do site e o do jogo são o mesmo backend (API tRPC em
`baiakidle.com/api/trpc`). Endpoints usados (GET, sem login):

- `auction.config` - taxas: **rake de 10% sobre a venda** + 5.000.000 gold de
  taxa de listagem (≈ 1 coin pela cotação atual); preço inicial mínimo 25
  coins; soft-close de 60s.
- `auction.history` - vendas concluídas (`delivered`). **Janela deslizante de
  ~7 dias** (~13k registros). Filtro `q` por nome, `type` item/gold.
- `auction.browse` - leilões ativos, com `currentPrice` (lance atual) e `endsAt`.
- `auction.item` - estado de um único leilão (usado no acompanhamento minuto
  a minuto das posições).

### Normalização de preço

- **gold**: preço por 1.000.000 de gold (fungível).
- **empilhável** (poções, ervas, tokens...): preço por unidade.
- **equipamento**: preço por lote, chave = `nome + raridade + imbuements +
  upLevel` (atributos diferentes = mercados diferentes).

### Avaliação de equipamento (raridade + atributos)

Raridade no jogo = nº de slots de imbuement (Comum 0 → Mítico 5). Os dados
confirmam que raridade e certos atributos são grandes drivers de preço
(Lendário/Mítico median ~100, p75 300-420; onslaught ×2,0, crit dmg ×1,5,
exp/weapon atk ×1,4 vs baseline).

Como as variantes valiosas têm pouco histórico exato, o "preço justo" usa uma
**escada**:

1. `nome + raridade + atributos + upLevel` (variante exata), `n >= min_sales`;
2. `nome + raridade` (qualquer build), `n >= min_sales_loose` - a estimativa
   é **ajustada** por `attr_score do item / attr_score mediano da cesta`
   (limitado a `[attr_adj_min, attr_adj_max]`) **e** multiplicada pelo
   **nível dos atributos** (`attr_level_mult`, abaixo);
3. sem dados → não estima, não alerta. `upLevel > 0` exige nível 1 da escada.

O painel mostra, embaixo do nome: **vocação · nível · Classe** (a classe 1-4
do item, do bundle), os **atributos com o valor real** (ex.: `Crit Damage +6%
(Lv.4)`, formato igual ao do jogo) + `attr_score`, e a linha **⚒ Forja Tn
(+x% Onslaught/Momentum/Ruse/…)** quando o item foi forjado (`ftier`). A base
usada pro preço justo também aparece.

`ftier` (forja) e `upLevel` (+n) entram na chave do item: forjado/refinado é
mercado à parte e a escada exige histórico exato dessa variante.

`attr_score` = Σ `attr_weights[id] × (1 + attr_level_factor × (nível-1))`.
Pesos default derivados dos multiplicadores observados (onslaught 3; crit dmg
/ weapon atk / exp / atk speed 2; frenzy / spell dmg 1,5; resto 0) - ajuste em
`config.json`. O score também mexe no rótulo: Raro+ com score 0 é limitado a
"boa"; score alto (`attr_score_strong`) sobe um nível.

**Valor dos atributos (modelo aprendido).** Quando a estimativa cai na cesta
`nome + raridade`, o ajuste pelos atributos vem de uma **regressão hedônica com
piso (Tobit)**: preço = efeito do (nome, raridade) × multiplicadores dos
atributos. Quase metade das vendas fecha no piso de 25 coins (o preço real
abaixo disso é invisível), então esses casos entram como censurados (EM); sem
isso o efeito real fica escondido. Ajuste em Python puro (~1 s), com cache de
`attr_model_hours` (1h) sobre `attr_model_days` (14) de vendas, atributos em
`attr_model_ids` e degraus do Exp 4+/6+/8+. Efeitos **multiplicam** (é o que
faz Exp valer +10 num item barato e mais combinado com Crit Damage). Medido:
Exp ×2,4 (Lv.4+ ×1,7, 6+ ×1,3, 8+ ×1,6 acumulado), Crit Damage ×1,5,
Onslaught ×1,4, Spell Damage ×1,2, Crit Chance/Loot ×1,1-1,2; Frenzy, Attack
Speed e Weapon Attack **sem prêmio mensurável** (os pesos antigos os
superestimavam). Testado fora da amostra (treino nos primeiros 60% do tempo):
o método antigo (`attr_score`) errava **mais** que a mediana simples nos itens
sem atributo premium (34% vs 20%); o modelo novo iguala ou melhora e acerta o
nível do Exp (Lv.4-8: 39/50/80/177/321 previsto vs 36/51/77/176/300 real).
**Interações** (pares) foram testadas e **não** melhoram a previsão: os efeitos
multiplicativos já explicam as combinações. A seção **Valor dos atributos** do
painel mostra a tabela, o Exp por nível e as combinações. `attr_score`
continua só nos rótulos/exibição.

**Calibração dos alertas de equipamento.** Comparando o "preço justo" mostrado
em cada alerta com o que o mesmo item (mesma classe de atributo) vendeu nos 3
dias seguintes (1.396 alertas fechados), o justo ficava **~1,4x acima** em todas
as raridades: a mediana da cesta acerta o mercado em geral (1,00x em 12 mil
vendas), mas o que parece barato costuma ser barato por um motivo (efeito
vencedor). Por isso o justo dos alertas de **equipamento** é multiplicado por
`gear_fair_calib` (0,70) - afeta desconto, rótulo, "Revender até" e economia.
Testado no histórico, também fora da amostra (metades no tempo): lucro real
total dos alertas de −6.133 pra +2.709 coins, com ~38% dos alertas. Usar o **p25**
como referência **não** se sustentou (soma ~0 e inconsistente entre as
metades). Por raridade, com o fator: 4-5 → +56 de lucro médio por alerta, 3 →
+18, **0-2 → sem vantagem** (−3,7 de média): esses seguem aparecendo, mas o
histórico não mostra lucro de revenda neles. O que já está no log de análises
antes dessa mudança ficou com o justo antigo. Não se aplica a lote, gold nem a
"Procurar por oferta".

**Boss Token** (bloco recolhível **dentro de Oportunidades - Lote (empilháveis)**,
porque boss token é um empilhável - não tem mais seção nem atalho próprio no
navbar; `analyze.boss_monitor`): na loja
(Comércio > Mercador > Boss Collector > Cosmetics > Addon Casket) **500 boss
tokens = 1 addon**, que custa **50 coins** - então cada token vale **0,10 coin
de uso** (o casket não é revendável; sem limite de compra). Lote de token que
fecha abaixo disso dá lucro. Cada leilão ativo mostra o **custo por 500 tokens**,
o **valor do lote** (addons inteiros valem cheio; a sobra vale
`boss_leftover_credit`, 1,0 = como se fosse usada em outro addon ou revendida a
~0,1/token), o **teto de lance** = valor × (1 − `boss_min_margin` 20%), a **chance de fechar
até o teto** e o **lucro esperado** de quem dá lance só até o teto. O leilão
começa no piso e sobe, e o desfecho é dividido em dois: ou ninguém disputa e
fecha no piso (1-2 lances), ou vira briga e passa do valor - por isso a
mediana do fechamento engana e é mostrada só como "Fecha típico". Chance e lucro
saem dos últimos `boss_recent_n` (20) fechamentos de lotes de tamanho parecido
(o lote nunca fecha abaixo do preço de agora): lucro esperado = chance × lucro
médio quando ganha; se o preço passar do teto você larga e não perde nada.
A tabela lista **só os lotes que compensam** (lucro esperado >=
`boss_min_ev`, 5; o que está fora da margem não aparece), os `boss_show_n` (10)
que **encerram primeiro**. Perto do fim (últimos
`alert_window_seconds`) o preço de agora já é o real. O card **Arbitragem** compara
o custo mediano de 500 tokens nos últimos `boss_recent_n` fechamentos com os 50
do addon: **aberta** (< 80%), **apertada** (< 100%) ou **fechada**; a tabela por dia mostra a mudança de regime
(até 20/09 500 tokens custavam ~13-25 coins com ~1 lance por leilão; no dia da
atualização foi pra ~60, com ~6 lances). Usa o snapshot da varredura (2 min), sem
requisições extras. Config: `boss_token_name`, `boss_tokens_per_addon`,
`boss_addon_coins`, `boss_min_margin`, `boss_min_ev`, `boss_show_n`, `boss_leftover_credit`, `boss_recent_n`.

**Codex x Market** (seção própria + atalho "Codex" no navbar; `codex.py`): o Codex
da conta tem 686 entradas (hunts, bosses e equipamento), cada uma pedindo itens
em quantidade (os degraus II e III de uma hunt pedem 5x e 15x o do degrau I).
Muita coisa que falta é empilhável barato no market - muitas vezes sai mais
rápido comprar o lote do que ficar na hunt. Como só o bot enxerga o Codex
(precisa do jogo logado), o fluxo é: **ao abrir o market** o bot lê o Codex da
conta - **só leitura, nunca entrega nem desbloqueia nada** - e grava
`codex_progress.json` ao lado do banco (cada entrada com recompensa, status,
progresso e os requisitos `tem/precisa`, já descontando o que está nas suas
bags); o market lê esse arquivo e cruza com os leilões de empilháveis ativos e o
histórico de preço. A leitura automática ao abrir só acontece se o jogo já está
aberto (bot rodando ou navegador com depuração) e se o último mapa tem mais de
10 min - ela **nunca abre o navegador sozinha**; o botão **Atualizar Codex** do
painel ignora esse intervalo (só funciona com o market aberto pelo bot - rodando
o `server.py` sozinho a seção avisa que precisa do bot). Hunts e bosses são
lidos; equipamento fica de fora (os requisitos dele são peças, não empilháveis).
A seção tem:
- **Priorizar efeito**: chips com os efeitos da recompensa (Dano crítico,
  Onslaught, Chance de crítico, Ataque, Dano de magia...). Sem nenhum marcado
  mostra todas as entradas abertas; marcando, só entram as entradas que dão
  aquele efeito. A escolha fica salva.
- **O que comprar no market**: lista de compras somada nas entradas escolhidas -
  por item, quanto falta, o preço justo por unidade, e os leilões de agora (qtd,
  preço, desconto vs o justo, quanto da necessidade o lote cobre, quando fecha)
  com **Dar lance** e **acompanhar**. Leilão novo começa no piso de 25 coins, então
  o aviso "preço de largada, ainda pode subir" aparece enquanto faltar mais que
  `alert_window_seconds`.
- **Entradas abertas**: por entrada, a recompensa (efeito priorizado marcado com
  ★), o progresso e o que falta de cada item com o custo **pelos lotes reais**
  (cada lote tem piso de 25 coins; leilão novo usa o preço típico, nunca abaixo
  do atual), mais baratas de fechar primeiro. "Incluir entradas bloqueadas"
  traz os degraus que ainda precisam de gold pra desbloquear (mostra o preço).
Config: `codex_days` (14, janela do preço justo), `codex_max_entries` e
`codex_max_items` (40 cada, o que a seção lista). O arquivo `codex_progress.json`
é pessoal (fica fora do git, do zip de compartilhamento e do artefato do Mac).
O item que o Codex pede e o market vende têm o mesmo nome; item sem histórico
suficiente (menos de `min_sales_loose` vendas) aparece como "sem preço".

**Avaliar item** (seção própria + link "Avaliar" no navbar): informe o **nº de um
leilão** (aceita `#359654`) e o sistema decompõe o valor pelas especificações
(`analyze.appraise`, `/api/appraise`, só leitura via `auction.item`): **base do
item e raridade sem atributos × multiplicador de cada atributo × degraus do nível
do Exp**, com o preço acumulado a cada passo. Mostra o **valor estimado**
(mediana do modelo), a **faixa provável** (p10-p90 pela dispersão do modelo), a
**referência conservadora (p25)** - o que se costuma conseguir de fato ao
revender (o "preço justo" dos alertas ficava ~1,4x acima do vendido nos dias
seguintes; ver Calibração) - e o **teto pra revender** (conservador - rake -
taxa). Traz também as **6 vendas mais parecidas** (nome + raridade, por nível
do Exp e atributos premium) e **avisos de confiança**: poucas vendas do item,
poucas vendas com Exp naquele nível (extrapolação), atributos fora o Exp em
nível alto e leilão ainda no início (preço de largada). Nota: o prêmio de
**nível dos atributos que não são Exp** deu ×1,23 por atributo em nível 4+
dentro da amostra, mas **não melhorou a previsão fora da amostra** (erro 41%
vs 38%), então **não entra** na conta; fica coberto pela faixa provável.

**Correção: item raro com atributo forte ficava sem valorizar** (2026-09-23).
Um item pouco vendido (poucas vendas na janela de `history_days`, 7 dias) caía
fora do fallback "raridade" e ia pra estimativa por tipo, que **ignora os
atributos** quando não há venda comparável suficiente - um soulbiter com Exp
Lv.6 aparecia com o mesmo preço de um sem atributo nenhum. Duas correções,
**validadas com backtest** (60/40 no tempo, sem regressão em nenhum grupo):
(1) quando a janela normal (`history_days`, 7 dias) não tem `min_sales_loose`
vendas, a cesta tenta de novo com janelas cada vez mais largas
(`basket_widen_days`, 14 e depois 30 dias) antes de desistir; (2)
`attr_model_min_group` caiu de 8 para 3, então mais itens raros
passam a ter sua própria base no modelo em vez de cair no ajuste antigo (sem
nível). Resultado no backtest: erro mediano geral 15% → 13%, em itens com
atributo premium 22% → 21%, e itens que antes ficavam sem preço passam a ter
um (mais 25 itens ultra-raros só acham amostra além de 14 dias). O banco ainda tem
~24 dias de histórico, então o passo de 30 dias por enquanto raramente muda o
resultado do de 14 - ele existe pronto pra quando o banco acumular mais tempo. **Testei uma abordagem mais
agressiva primeiro** (multiplicador global do modelo direto sobre a cesta,
ignorando a base já calibrada) **e descartei**: piorava muito (MAPE 81% vs 41%)
justamente nos itens raros com Exp alto, porque a cesta de 3-14 vendas é ruim
demais pra normalizar por ela mesma. Caso real testado: soulbiter (Épico,
Loot Lv.1 + Exp Lv.6 + Spell Damage Lv.5) foi de **30 coins** (estimativa por
tipo, sem atributos) para **891 coins** (base "raridade", n3).

**Nível do atributo (Exp 4%, 6%, 10%...).** O nível máximo depende da raridade
(4/6/8/10/12 nas raridades 1-5) e o vendedor pode ter gasto coins subindo o
atributo, o que encarece o item. Medido no histórico (mesma peça, só o Exp
subido, vs. nível 1): nível 2-3 ≈ 1,0x, nível 4 ≈ 1,5x, 5 ≈ 1,65x, 6 ≈ 2,4x,
7 ≈ 3,5x, 8 ≈ 3,8x, e acima disso cresce muito (poucas vendas). Só o Exp mostra
prêmio de nível; nos outros atributos o nível não mudou o preço, então eles
valem por presença. A tabela fica em `attr_level_mult` (`{id: {nível: mult}}`;
nível acima do maior listado usa o maior) e é usada quando a estimativa cai na
cesta `nome + raridade` (a variante exata, com o nível na chave, já reflete o
nível). Backtest (últimos 40% no tempo, itens com Exp >= 4): viés mediano
+26 → +2 coins, e o previsto acompanha o real nos níveis 5-8.

**Comprar já upado ou upar sozinho?** Upar o Exp custa gemas (tentativa e
erro; 10 gemas = 150M gold, `gem_gold` = 15M por gema). Em média, acumulado:
Lv.4 ≈ 10 gemas, 5 ≈ 14, 7 ≈ 20, 8 ≈ 33, 10 ≈ 55, 12 ≈ 115 (`exp_upgrade_gems`,
ajuste no `config.json`). As gemas viram coins pela **taxa fresca do gold**
(`gold_rate`). Pra cada item com Exp > 1, o painel compara o preço do item
pronto com `preço justo do nível 1 + custo de upar` e mostra, embaixo dos
atributos, qual sai mais barato (verde = comprar pronto, laranja = upar
sozinho). Em "Procurar por oferta" o **Usar até** é limitado por esse custo
(não vale pagar mais do que upar você mesmo); o **Revender até** não muda (o
seu comprador não liga pro seu custo de upar). No histórico, até o Exp 6 o
mercado cobra bem menos pelo item pronto (+11, +24, +32 coins) do que custa
upar (27, 37, 45), e do 7 pra cima inverte.

**Oportunidade Compra/Venda** (seção própria no painel; comprar o nível 1, upar e revender). A outra ponta: comprar o item
com Exp no nível 1 (só dá pra upar quem já tem o atributo), gastar gemas e
vender o item upado. **Só entra o que está no mercado agora** (leilão ativo de
nível 1 com Exp); o histórico é a referência de preço do item upado. Uma linha
por (nome, raridade): o melhor nível-alvo L (maior lucro no leilão mais barato)
e os demais como alternativa. **Venda esperada** = mediana das vendas com Exp em
L-1..L+1 (as gemas dão resultado incerto, então o nível de baixo entra); custo =
gemas esperadas na taxa do gold (`gold_rate`). **Comprar até** = venda x (1 -
rake) - taxa de listagem - gemas - `craft_min_profit` (o preço máximo do nível 1
que ainda deixa a margem). A linha só aparece se o leilão mais barato passa em
`craft_min_profit` e `craft_min_p` (fatia das vendas comparáveis ainda
lucrativas), com pelo menos `craft_min_upgraded` vendas nos níveis vizinhos
(janela `craft_days`). **Preço de compra:** leilão com mais de `alert_window_seconds` pela frente
ainda está perto do piso, então usa-se o fechamento típico do nível 1 com Exp
(mediana das vendas), se for maior (ex.: "agora 26 → deve fechar ~110"). Cada
leilão mostra o lucro mediano, o **pior** e o **melhor** das vendas observadas
do item upado, o **se não render** (as gemas não passam do nível 3: vende como
nível 1), quando fecha e quando você venderia (fecha + 6h de anúncio mínimo).
**Concorrência na hora da venda:** pra cada leilão, o sistema estima quando o
seu anúncio fecharia (fecha + 6h) e conta os anúncios **iguais** (mesmo nome +
raridade) já ativos que fecham numa janela de `flip_horizon_hours` em volta,
contra a média de vendas/dia do item. Sem rivais: **vender logo**; pressão
<= 1,2: bom momento; <= `flip_max_pressure`: ok; acima: **mercado cheio -
esperar**, com a **melhor janela** (até 24h depois) e a hora de anunciar.
`analyze._sell_market`. Só enxerga o que já está listado. No histórico de ~3 semanas quase nada
fecha e as amostras têm 3-4 vendas (~0,1/dia): pista, não garantia. Backend:
`analyze.craft_table`, `server._craft`.
Abaixo da tabela há uma subseção recolhível **"Outros itens no mercado"**: os
itens de nível 1 com Exp que **não passaram** nos filtros, com o **motivo**
(amostra insuficiente do item upado / lucro abaixo do mínimo / chance abaixo do
mínimo) e os números do melhor nível-alvo (lucro, comprar até, venda, chance),
ordenados do mais perto de passar pro mais longe - pra você avaliar se algum
faz sentido e calibrar `craft_min_upgraded`, `craft_min_profit` e
`craft_min_p`. Paginada (10 por página).

### Regra de oportunidade

Um leilão ativo entra na lista quando **todas** valem:

1. histórico suficiente: `>= min_sales` vendas e `>= min_sales_per_day`/dia;
2. `preço_unidade <= mediana * (1 - discount_threshold)`;
3. preço total do lote em `[budget_min_coins, budget_max_coins]`;
4. `>= min_bids` lances;
5. encerra entre `min_seconds_left` e `alert_window_seconds`.

O ponto 5 evita ruído: todo anúncio começa em 25 coins com 0 lances, então só
perto do fim o preço atual significa algo. Padrão: só olha os **últimos 10
min** de cada leilão (`alert_window_seconds` 600) - é quando o preço de fato
se desenvolve.

**Pisca-verde (oportunidade excelente de última hora)**: equipamento
**Lendário+** (`flash_min_rarity` 4) com **gap ≥ 20 coins** entre preço atual
e "revender até" (`flash_min_gap_coins`) fica na lista até o fim (ignora o
piso de `min_seconds_left`) e, no **último minuto** (`flash_max_seconds` 60),
a linha **pisca em verde**. Essas ficam marcadas no histórico (`flash`) e o
**Histórico de análises** mostra só elas (últimas 24h) com o resumo próprio
(quantas fecharam baratas, lucro que passou, ROI).

### Revender até / Usar até

Cada equipamento mostra dois tetos de preço, pros dois objetivos:

- **Revender até** = `preço_justo × (1 - discount_threshold)` - compre até aqui
  e revende com margem folgada. O subtítulo **"empata N"** = `preço_justo ×
  (1 - rake)`: pagando acima disso você **perde** ao revender (rake 10%).
- **Usar até** = o **preço justo** (mediana). Até aqui você equipa sem pagar
  acima do mercado; o rake não te afeta porque só incide na venda.

O **Preço atual** vem colorido: **verde** = já dá pra revender com lucro
(≤ "Revender até"), **âmbar** = só compensa se for equipar (entre os dois
tetos). O `-N%` embaixo é o desconto vs preço justo no preço atual.

### Gold é tratado à parte (só pra USAR)

Gold é um mercado **ancorado numa taxa única** (coins por 100M de gold) que
muda por regimes - ex.: caiu de ~24 pra ~18 em poucos dias. Acima de ~150M o
preço por 100M é o mesmo em qualquer tamanho; só lote pequeno sofre o **piso de
25 coins** (77% dos lotes ≤100M fecham no piso). O modelo (medido em ~15 mil
vendas):

1. **Taxa fresca** = mediana exponencial (meia-vida `gold_rate_halflife_h`,
   12h) das vendas de lotes > `gold_rate_min_lot` (150M). Foi o estimador que
   melhor acompanhou o mercado (viés ~0 na queda; a mediana de 7 dias errava
   ~11% pra baixo).
2. **Preço esperado do lote** = `max(25, taxa × lote/100M)`. Não há mais as 3
   faixas p/m/g.
3. **Desconto agora** = `1 - preço/esperado`. Entra se `>= gold_discount_threshold`
   (0.15) e a economia `>= gold_min_saving_coins`. O painel mostra em coins/100M.
4. **Desconto ao fechar**: o preço que se vê 10 min antes do fim **sobe** (79% das
   oportunidades subiram, mediana +24%), então o desconto real encolhe e satura
   em ~20% não importa quão barato pareça. A curva `gold_curve_exp` (medida no
   histórico) converte o desconto de agora no que se costuma pagar de verdade, e
   `gold_curve_p` dá a chance de ainda sobrar `>= gold_use_min_discount` (10%).
5. **Teto de lance** = `taxa × (1 - gold_use_min_discount)`: acima disso o
   desconto de uso deixa de compensar.
6. **Qualidade** = pelo desconto **esperado ao fechar** (`gold_exp_great` 14%,
   `gold_exp_excellent` 18%), e desce um nível se a taxa está em queda forte
   (`trend <= trend_down`, taxa agora vs 48h atrás). Backtest: boa → excelente
   sobe o desconto realizado médio de 9% pra 19% e derruba a chance de pagar
   acima da taxa de 22% pra 17%.
7. Lote com **preço redondo** (exatamente 20 ou 25 por 100M) recebe a marca
   "preço redondo": é preço fixado pelo vendedor, tende a não subir até fechar
   (o desconto que você vê é o que você leva). Não é filtrado - nos dados, ele
   só aparece com desconto pequeno.

**Momento do mercado** (card acima da tabela de Gold): taxa agora, variação
24h/7d, posição da taxa nos últimos 14 dias (percentil, 0 = mais barato),
volatilidade diária e um gráfico. "Bom momento" = taxa barata (≤ percentil 25)
**e** sem queda forte em 24h; "barato, mas ainda caindo" avisa que pode
baratear mais.

**Snapshots**: a cada varredura, os leilões de gold nos últimos
`gold_snap_window_min` (30) minutos são gravados na tabela `gold_snap` (preço,
lances, tempo restante; só quando algo muda), guardados por `gold_snap_keep_days`
(60). Junto com o preço final (`sales`), é a base pra treinar depois "vai fechar
abaixo do meu teto?" já usando os lances - o que hoje não temos na hora da
detecção. No histórico de análises o gold é contado à parte (fechou no teto /
abaixo da taxa / acima da taxa), sem rake.

### Rótulo de qualidade

Equipamento e lote: pelo desconto (`tier_great` 42%, `tier_excellent` 60%).
Gold: pelo desconto esperado ao fechar (acima). Nos dois casos, preço em
queda (`trend <= trend_down`) desce um nível.

## Configuração

Copie `config.example.json` para `config.json` e ajuste. Principais campos:

| campo | o que é |
|---|---|
| `budget_min_coins` / `budget_max_coins` | faixa de preço total do lote |
| `discount_threshold` | desconto mínimo vs mediana (0.25 = 25%) |
| `min_sales` / `min_sales_per_day` | filtro de liquidez |
| `alert_window_seconds` | só considera leilões encerrando dentro desse prazo |
| `watchlist` | se preenchida, só itens cujo nome contém um dos termos |
| `trend_min_recent` | vendas nos últimos 2 dias pra calcular tendência (4) |
| `trend_down` / `trend_up` | limiar de queda / alta de preço (-0.10 / +0.10) |
| `deduct_listing_fee` | descontar a taxa de listagem (5M gold) do lucro |
| `telegram_bot_token` / `telegram_chat_id` | notificação push (opcional) |
| `http_port` | porta do painel |

### Telegram (opcional)

Crie um bot com o @BotFather, pegue o token. Mande uma mensagem pro bot e
veja o `chat_id` em `https://api.telegram.org/bot<TOKEN>/getUpdates`.

## Arquivos

| arquivo | função |
|---|---|
| `api.py` | cliente da API tRPC (read-only) |
| `store.py` | SQLite: vendas, oportunidades, posições, settings |
| `analyze.py` | normalização, mediana/percentil, liquidez, regra |
| `scanner.py` | coleta + scan + acompanhamento de posições (compartilhado) |
| `items.py` / `items.json` | tabela de itens do jogo (slot/level/vocação) p/ o filtro por personagem |
| `notify.py` | console + Telegram |
| `run.py` | CLI |
| `server.py` | painel web + poller em background |
| `dashboard.html` | interface do painel |
| `market.db` | banco (criado na 1ª execução; backfill de ~7 dias) |
| `backups/` | cópias de `market.db` feitas pelo `server.py` na inicialização (últimas 15) |

Posições e vendas nunca são apagadas pelo sistema. Testes usam um banco
separado (`BAIAK_DB=market.test.db python server.py`).

## Desenvolvimento: alinhamento com o bot

O `market/` é parte do produto "bot" (mesmo repositório, mesma versão): ele roda
embutido no bot e **só chega aos outros PCs/Macs numa release nova** (bump de
`bot.VERSION` + build + release no GitHub). O passo a passo completo e o
protocolo de alinhamento entre as conversas de desenvolvimento (Bot e Market)
estão no **`RELEASING.md`**, na raiz do repositório (não vai no build).

Quem mexe em quê:

| área | dona |
|---|---|
| `market/*` (código, dashboard, scanner, api, store) | Market |
| leitura do Codex pro market (`read_codex_progress` e afins no `bot.py`) | Market, usando os helpers do Bot |
| `gui.py`, rotinas, Campanha de Codex, Poções, atualizador | Bot |
| `build.bat`, `build-macos.yml`, `make_share_zip.py`, `.gitignore`, `RELEASING.md` | compartilhado: mudou, avise a outra conversa |

O essencial ao mexer aqui:

- **Arquivo novo de dados/pessoal** no `market/`: entra nas TRÊS listas de exclusão
  (`.gitignore`, `MARKET_EXCLUDE_NAMES` em `make_share_zip.py` e o `--exclude` do rsync
  em `build-macos.yml`). Nunca vai no build: `market.db*`, `backups/`, `chrome_profile/`,
  `config.json`, `codex_progress.json*`.
- **Módulo novo importado pelo market** (stdlib ou pacote): `--hidden-import` em
  `build.bat` e `build-macos.yml` - o PyInstaller não enxerga os imports do market
  (carregado em runtime).
- **Helpers compartilhados do `bot.py`** (`open_codex`, `codex_hunts_view`,
  `read_codex_hunt_entries`, `build_codex_entry`, `request_from_bot`, `load_state`):
  não mude o comportamento sem checar quem usa; prefira adicionar função nova.
  Os filtros do Codex ficam sempre ligados ao terminar de ler.
- Ao fechar uma mudança relevante, escreva a **nota de handoff** do `RELEASING.md`
  pro usuário levar à outra conversa.

## Limitações

- Histórico da API é só ~7 dias - deixar o `server.py`/`watch` rodando
  contínuo acumula base além disso.
- Sem login, não dá pra saber automaticamente se você ganhou um leilão -
  você confirma manualmente (o painel mostra o preço final pra ajudar).
- `auction.history` só traz o que **vendeu**; leilões que expiraram sem lance
  não entram (o "preço justo" fica levemente otimista).
- Não detecta manipulação (lavagem entre contas, erro de digitação). P25 e o
  filtro de liquidez ajudam, não resolvem.
- Acesso automatizado pode violar os Termos de Serviço do jogo mesmo sendo
  leitura. Risco assumido pelo usuário.
