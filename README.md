# Union Veículos — Painel da social media

App local que acompanha o estoque do site <https://www.unionrioveiculos.com.br>:

- **Carros novos (F1):** a cada 15 min o site é consultado; cada carro novo vira um
  *chamado pendente* com foto no painel. O botão **Feito** marca o carro como postado.
- **Queda de preço (F2):** todo dia às **18:00 (America/Sao_Paulo)** os preços são revisados
  (casando por id do anúncio e, como fallback, pelo nome). Quedas geram alertas
  "preço antigo → preço atual" no painel. Todo preço fica no histórico.
- **Carro vendido (F3):** carro que some do site por 2 coletas seguidas (~30 min) é marcado
  como vendido: aviso no painel + notificação, e o chamado pendente dele é cancelado.
  Se voltar ao site, é reativado sozinho.
- **Editor de encarte (F4):** em `/editor`, escolha um carro e gere o post do Instagram
  (1080x1350): capa com a foto principal na moldura + fotos seguintes, legenda pronta e
  download em ZIP.

Stack: Python 3.13 · FastAPI · SQLite · APScheduler · httpx + BeautifulSoup · Jinja2 + HTMX.

## Rodar (Windows, jeito fácil)

Dê dois cliques em **`run.bat`**. Ele cria o `.venv`, instala as dependências, sobe o
servidor e abre <http://127.0.0.1:8000> no navegador. Deixe a janela aberta: os jobs só
rodam enquanto o app estiver no ar.

## Rodar manualmente

```bat
py -3.13 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Testes: `pip install -r requirements-dev.txt` e depois `pytest`

## Como funciona

- Banco: `data/union.db` (SQLite, criado sozinho e migrado sozinho em versões novas).
  Tabelas `cars`, `tickets`, `price_history`, `price_alerts`, `sold_alerts`, `runs` (log de
  cada execução).
- **Primeira execução:** com o banco vazio, o estoque atual entra como *já postado*
  (baseline), sem gerar chamados. Daí em diante só carros novos geram chamado.
- Se o site falhar, a execução é registrada como erro e nada é alterado.
- Notificação do Windows (winotify) ao surgir carro novo, queda de preço ou carro vendido.
- Botões de admin no painel: *verificar agora* e *revisar preços agora*.
- `GET /health` mostra os jobs agendados e as estatísticas.

## Painel da Equipe (`/equipe`)

Tela para os funcionários (celular), somente leitura: carros em estoque ordenados por
dias em estoque, com sugestão de redução de preço. Regras em `app/services/aging.py`
(constantes `TIERS`):

- **Relógio** = dias desde a última redução de preço (subida não conta), ou desde a
  entrada se nunca baixou. 30–44 dias → −3%; 45–59 → −5%; 60+ → −8%.
- **Preço sugerido** arredondado para baixo terminando em 900 (R$ 126.003 → R$ 125.900).
- **Entrada:** data de cadastro obtida do site quando disponível; senão o dia em que o app
  viu o carro pela 1ª vez. Carros do registro inicial (1ª coleta) mostram
  "há pelo menos X dias", porque a entrada real é anterior.

## Modo demo

Para mostrar o painel com exemplos (chamados, queda de preço, vendido):

```bat
.venv\Scripts\python -m app.demo seed    :: cria/recria dados demo
.venv\Scripts\python -m app.demo clear   :: remove só os dados demo
```

Os dados demo usam `external_id` com prefixo `demo-` e não interferem na detecção real.

## Fontes do encarte

Padrão (PC e nuvem): **Saira**, fonte livre (licença OFL, em
`app/assets/fonts/saira/OFL.txt`), empacotada no repositório para a arte sair igual em
qualquer lugar. Se os arquivos da **TT Lakes Neue** (`.ttf`/`.otf`, nome contendo "Lakes")
forem colocados em `app/assets/fonts/`, eles passam a ter prioridade — só use uma versão
licenciada para uso comercial. Toda a configuração (peso/largura por texto) fica em
`app/encarte/fonts.py` (`FONTS`).

## Deploy na Vercel (Hobby) + Neon Postgres

Na nuvem o app roda como uma Vercel Function (detecção automática do FastAPI em
`app/main.py`, variável `app`). Diferenças em relação ao app local:

- **Banco:** Postgres (Neon) via `DATABASE_URL`. Sem ela, o app usa SQLite (local).
  As tabelas são criadas/migradas sozinhas no primeiro acesso.
- **Agendamento:** o APScheduler fica desligado (`VERCEL=1`). Os jobs são rotas HTTP
  protegidas por `Authorization: Bearer <CRON_SECRET>`:
  - `GET /cron/price-check` — Vercel Cron diário `0 21 * * *` (21:00 UTC = 18:00 BRT,
    configurado no `vercel.json`). **No plano Hobby a Vercel pode disparar em qualquer
    minuto dessa hora** (18:00–18:59).
  - `GET /cron/poll` — a cada 15 min pelo GitHub Actions (`.github/workflows/poll.yml`),
    porque o Hobby só permite cron diário.
  - Respostas JSON: `200 {ok: true, job, result}`; falha no site → `502 {ok: false, error}`;
    Bearer errado → `401`; `CRON_SECRET` ausente → `503`.
- **Login com dois perfis** (mesma tela; a senha decide o perfil):
  - **admin** (`APP_PASSWORD`, social media): acesso total.
  - **equipe** (`STAFF_PASSWORD`, funcionários): só o **Painel da Equipe** (`/equipe`, somente
    leitura: dias em estoque e sugestões de redução de preço). Sem `STAFF_PASSWORD`, o perfil
    equipe fica desativado. Trocar a senha derruba as sessões da equipe.
  - Na Vercel sem `APP_PASSWORD` o app responde 503. Local sem senhas → tudo aberto (admin).
- **Arquivos:** só `/tmp` é gravável (cache de fotos). O detalhe do anúncio fica em cache
  na tabela `detail_cache` (10 min), então as 11 prévias do editor fazem um único acesso
  ao site mesmo em instâncias diferentes.
- **ZIP do encarte:** montado no navegador (limite de 4,5 MB por resposta da Vercel).
- **Avisos:** WhatsApp via CallMeBot (sem notificação do Windows).

### Variáveis de ambiente (Settings → Environment Variables, ambiente Production)

| Variável | Obrigatória | Valor |
|---|---|---|
| `DATABASE_URL` | sim | Injetada pela integração Neon (aceita também `POSTGRES_URL`). Use a URL *pooled*. |
| `APP_PASSWORD` | sim | Senha do painel (perfil admin). |
| `STAFF_PASSWORD` | opcional | Senha dos funcionários (perfil equipe: só `/equipe`). Deve ser diferente da `APP_PASSWORD`. |
| `SESSION_SECRET` | sim | Texto aleatório longo (assina o cookie de login). |
| `CRON_SECRET` | sim | Texto aleatório com 16+ caracteres (a Vercel Cron envia no header). |
| `APP_URL` | recomendada | URL de produção, ex. `https://union-painel.vercel.app` (links no WhatsApp). |
| `WHATSAPP_PHONE` | opcional | Ex. `5521999999999`. |
| `CALLMEBOT_APIKEY` | opcional | Chave do CallMeBot. Sem ela (ou sem o telefone), não há aviso. |

### Passo a passo

1. Suba o repositório para o GitHub e importe o projeto na Vercel (Framework: FastAPI,
   detectado sozinho). Não precisa de Build Command.
2. **Storage → Neon → Create/Connect** e conecte ao projeto (isso cria `DATABASE_URL`).
3. Cadastre `APP_PASSWORD`, `SESSION_SECRET`, `CRON_SECRET`, `APP_URL`, `STAFF_PASSWORD` (e, se quiser,
   `WHATSAPP_PHONE` + `CALLMEBOT_APIKEY`). Faça **Redeploy** depois de mudar variáveis.
4. **Deployment Protection:** se a proteção (Vercel Authentication) estiver ligada no
   domínio de produção, o GitHub Actions leva 401 em `/cron/poll`. Deixe a produção
   pública (o app já tem login próprio) ou use *Protection Bypass for Automation*.
5. No GitHub: **Settings → Secrets and variables → Actions** → secrets `APP_URL` e
   `CRON_SECRET` (mesmos valores da Vercel). Rode o workflow uma vez em *Actions →
   Run workflow* para testar.
6. Primeira execução do poll no banco novo: o estoque atual entra como já postado
   (baseline), sem chamados.

Limites do Hobby considerados: função até 60 s (`maxDuration` no `vercel.json`),
resposta até 4,5 MB (cada slide PNG tem ~1,6 MB), 1 cron por dia.

## Variáveis de ambiente (app local)

| Variável | Efeito |
|---|---|
| `UNION_DB_PATH` | caminho do SQLite (padrão `data/union.db`) |
| `UNION_DISABLE_SCHEDULER=1` | não inicia os jobs (testes) |
| `UNION_MOCK_SCRAPER=1` | usa dados falsos em vez do site |
| `UNION_DISABLE_NOTIFY=1` | desliga notificações do Windows |
| `UNION_POLL_MINUTES` | intervalo do poll (padrão 15) |
| `UNION_DATA_DIR` | pasta de caches (padrão `data/`; na Vercel `/tmp/union`) |
| `DATABASE_URL` | usa Postgres em vez do SQLite (ver Deploy) |

## Estrutura

```
app/
  main.py        rotas FastAPI + templates
  db.py          conexão e schema SQLite
  models.py      Car, Ticket, PriceAlert
  scheduler.py   APScheduler (15 min + 18:00)
  scraper/       coleta do site
  services/      sync (carros novos), prices (queda), matching, queries, jobs, notify
  templates/ static/   painel (Jinja2 + HTMX)
tests/
```
