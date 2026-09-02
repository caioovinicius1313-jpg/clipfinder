# Ranking diário de vídeos por hashtag

## Como funciona

Para cada hashtag de projeto, o job:

1. chama `search.list` uma vez para encontrar até 25 candidatos;
2. reaproveita também os vídeos do snapshot anterior, evitando perder uma tendência;
3. chama `videos.list` em lote com `part=snippet,statistics`;
4. salva um snapshot diário no PostgreSQL;
5. calcula `view_delta` somente quando existe um snapshot do dia anterior;
6. disponibiliza o ranking por crescimento em 24 horas ou views acumuladas.

Projetos com a mesma hashtag compartilham a chamada à API no mesmo job. O limite padrão é de 80 hashtags únicas por execução, reservando parte da cota para as pesquisas manuais do painel.

O link de mídia não é armazenado. Quando o usuário clica em **Baixar vídeo**, o backend executa yt-dlp com:

```text
--extractor-args "youtube:player_client=default,web_embedded"
```

e redireciona para a URL temporária retornada pelo YouTube. Essa URL pode expirar em poucas horas e a extração pode falhar em vídeos privados, restritos, não incorporáveis ou protegidos.

## Variáveis opcionais

```env
YOUTUBE_RANKING_METRIC=view_delta
YOUTUBE_RANKING_CANDIDATE_LIMIT=25
YOUTUBE_RANKING_TOP_LIMIT=10
YOUTUBE_RANKING_MAX_HASHTAGS_PER_RUN=80
YT_DLP_TIMEOUT_SECONDS=60
```

`view_delta` é o padrão recomendado para vídeos em alta. Use `views` se quiser ordenar sempre pelo total acumulado.

## Executar manualmente

Depois de instalar as dependências e aplicar as migrations:

```powershell
cd C:\Users\Lenovo\Downloads\Clipfinder\backend
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m app.jobs.sync_hashtag_rankings
```

O comando imprime um resumo em JSON e encerra. Isso permite usá-lo como Cron Job sem manter outro servidor ligado.

## Configurar na Railway

Crie um segundo serviço usando o mesmo repositório GitHub. Nome sugerido:

```text
hashtag-ranking-cron
```

Configure:

```text
Root Directory: /backend
Config File Path: /backend/railway-ranking-cron.json
Builder: Railpack
```

Adicione as variáveis:

```env
DATABASE_URL=${{Postgres.DATABASE_URL}}
YOUTUBE_API_KEY=${{clipfinder.YOUTUBE_API_KEY}}
YOUTUBE_RANKING_METRIC=view_delta
YOUTUBE_RANKING_CANDIDATE_LIMIT=25
YOUTUBE_RANKING_TOP_LIMIT=10
YOUTUBE_RANKING_MAX_HASHTAGS_PER_RUN=80
YT_DLP_TIMEOUT_SECONDS=60
```

Se os serviços do PostgreSQL ou backend tiverem outros nomes, ajuste as referências.

Em **Settings → Cron Schedule**, coloque:

```text
0 9 * * *
```

O Railway trabalha em UTC. Esse exemplo executa diariamente às 09:00 UTC, equivalente a 06:00 no horário de Brasília. O serviço não precisa de domínio, volume ou bucket e encerra sozinho ao terminar.

Para gerar o primeiro snapshot sem esperar o dia seguinte, use temporariamente `*/5 * * * *`, espere uma execução concluir e depois volte para `0 9 * * *`.

## Cota do YouTube

O job faz uma busca e uma consulta de estatísticas por hashtag única. Limitar candidatos a 25 mantém a busca e os vídeos anteriores dentro do limite de 50 IDs aceito pelo `videos.list`. Monitore a cota no Google Cloud e reduza `YOUTUBE_RANKING_MAX_HASHTAGS_PER_RUN` se necessário.

## Observação sobre direitos de uso

O botão de download deve ser usado somente para conteúdo próprio, licenciado ou autorizado. O ClipFinder não remove restrições do YouTube e não garante que todo vídeo possa ser extraído.
