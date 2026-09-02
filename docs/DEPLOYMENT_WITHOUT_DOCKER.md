# Publicar o ClipFinder sem Docker

Arquitetura usada:

- **GitHub:** código-fonte.
- **Railway:** FastAPI, PostgreSQL, Railway Bucket e volume temporário.
- **Vercel:** frontend Next.js.

## Segurança

A API ainda não possui login. Enquanto a autenticação não for implementada, qualquer pessoa com o endereço poderá criar e excluir projetos. Não divulgue o site publicamente.

## Backend e banco na Railway

1. Crie um projeto na Railway.
2. Adicione um PostgreSQL.
3. Importe o repositório GitHub como um novo serviço.
4. Configure o serviço:
   - **Root Directory:** `/backend`
   - **Config File Path:** `/backend/railway.json`
   - **Builder:** Railpack
5. Em **Settings → Networking**, gere um domínio público.

### Variáveis básicas do backend

```env
DATABASE_URL=${{Postgres.DATABASE_URL}}
STORAGE_PATH=/app/storage
CORS_ORIGINS=https://SEU-FRONTEND.vercel.app,http://localhost:3000
YOUTUBE_API_KEY=
YOUTUBE_RANKING_METRIC=view_delta
YOUTUBE_RANKING_CANDIDATE_LIMIT=25
YOUTUBE_RANKING_TOP_LIMIT=10
YOUTUBE_RANKING_MAX_HASHTAGS_PER_RUN=80
YT_DLP_TIMEOUT_SECONDS=60
```

## Railway Bucket para vídeos grandes

1. No canvas do projeto, clique em **Create → Bucket**.
2. Use o nome `clipfinder-storage`.
3. No serviço `clipfinder`, abra **Variables**.
4. Adicione estas referências ao Bucket:

```env
BUCKET_NAME=${{clipfinder-storage.BUCKET}}
BUCKET_ACCESS_KEY_ID=${{clipfinder-storage.ACCESS_KEY_ID}}
BUCKET_SECRET_ACCESS_KEY=${{clipfinder-storage.SECRET_ACCESS_KEY}}
BUCKET_ENDPOINT=${{clipfinder-storage.ENDPOINT}}
BUCKET_REGION=${{clipfinder-storage.REGION}}
BUCKET_URL_STYLE=virtual
MAX_UPLOAD_SIZE_GB=10
```

Se o Bucket tiver outro nome no canvas, substitua `clipfinder-storage` nas referências. Nunca copie os valores secretos para o GitHub ou para o frontend.

Quando as credenciais estão presentes, o ClipFinder automaticamente:

- configura o CORS do Bucket com os endereços de `CORS_ORIGINS`;
- envia arquivos grandes em partes de 16 MB;
- armazena originais, referências e cortes no Bucket;
- mantém links e credenciais privados;
- remove os objetos ao excluir o projeto.

Confira a conexão abrindo:

```text
https://SEU-BACKEND.up.railway.app/health/storage
```

O resultado esperado é:

```json
{"storage":"bucket"}
```

### Volume temporário

Mantenha um volume montado em:

```text
/app/storage
```

Os arquivos permanentes ficam no Bucket. O volume continua útil para arquivos locais antigos e temporários. Cortes muito longos ainda podem exigir mais espaço temporário.

## Frontend na Vercel

1. Importe o mesmo repositório na Vercel.
2. Selecione **Root Directory** como `frontend`.
3. Use o preset **Next.js**.
4. Deixe **Output Directory** sem override.
5. Adicione:

```env
NEXT_PUBLIC_API_URL=https://SEU-BACKEND.up.railway.app
```

6. Publique o projeto.
7. Volte à Railway e coloque o endereço final da Vercel em `CORS_ORIGINS`.

## Job diário do ranking de hashtags

Depois que o backend estiver funcionando, crie um segundo serviço Railway a partir do mesmo repositório:

- **Nome:** `hashtag-ranking-cron`
- **Root Directory:** `/backend`
- **Config File Path:** `/backend/railway-ranking-cron.json`
- **Cron Schedule:** `0 9 * * *`

Adicione pelo menos:

```env
DATABASE_URL=${{Postgres.DATABASE_URL}}
YOUTUBE_API_KEY=${{clipfinder.YOUTUBE_API_KEY}}
```

Esse serviço não recebe tráfego público e encerra após cada snapshot. O procedimento detalhado está em [`HASHTAG_RANKING.md`](HASHTAG_RANKING.md).

## Verificação

- `/health` deve responder `{"status":"ok"}`.
- `/health/storage` deve responder `{"storage":"bucket"}`.
- `/docs` deve abrir o Swagger.
- Envie primeiro um arquivo pequeno e depois valide um upload grande.

## Custos

Armazenamento, CPU, memória e tráfego podem consumir créditos. Configure um limite de gastos na Railway e exclua projetos que não serão mais usados.
