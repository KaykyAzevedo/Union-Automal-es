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

Testes: `pytest`

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

## Modo demo

Para mostrar o painel com exemplos (chamados, queda de preço, vendido):

```bat
.venv\Scripts\python -m app.demo seed    :: cria/recria dados demo
.venv\Scripts\python -m app.demo clear   :: remove só os dados demo
```

Os dados demo usam `external_id` com prefixo `demo-` e não interferem na detecção real.

## Fontes do encarte

Coloque os arquivos da **TT Lakes Neue** (`.ttf`/`.otf`, nome contendo "Lakes") em
`app/assets/fonts/`. Enquanto não existirem, o app usa a Bahnschrift do Windows. Toda a
configuração de fontes fica em `app/encarte/fonts.py` (`FONTS`).

## Variáveis de ambiente

| Variável | Efeito |
|---|---|
| `UNION_DB_PATH` | caminho do SQLite (padrão `data/union.db`) |
| `UNION_DISABLE_SCHEDULER=1` | não inicia os jobs (testes) |
| `UNION_MOCK_SCRAPER=1` | usa dados falsos em vez do site |
| `UNION_DISABLE_NOTIFY=1` | desliga notificações do Windows |
| `UNION_POLL_MINUTES` | intervalo do poll (padrão 15) |

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
