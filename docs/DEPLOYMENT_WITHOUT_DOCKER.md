# Publicar o ClipFinder sem Docker

Esta é a arquitetura recomendada para a versão atual:

- **GitHub:** guarda o código.
- **Railway:** executa o backend FastAPI, fornece PostgreSQL e mantém os vídeos em um volume.
- **Vercel:** publica o frontend Next.js.

## Antes de publicar

A API atual ainda não possui login. Enquanto isso, qualquer pessoa que descobrir o endereço da API poderá tentar acessar seus endpoints. Não divulgue o endereço como aplicação pública até adicionar autenticação.

## 1. Enviar o projeto ao GitHub

Crie um repositório vazio no GitHub e envie a pasta `Clipfinder`. Os arquivos `.env`, vídeos e senhas já estão ignorados e não devem ser enviados.

## 2. Criar o backend na Railway

1. Crie um projeto vazio na Railway.
2. Adicione um PostgreSQL pelo botão **New → Database → PostgreSQL**.
3. Adicione um serviço usando o repositório do GitHub.
4. Nas configurações do serviço, informe:
   - **Root Directory:** `/backend`
   - **Config File Path:** `/backend/railway.json`
   - **Builder:** Railpack
5. Crie um domínio público em **Settings → Networking → Generate Domain**.

### Variáveis do backend

Na aba **Variables** do serviço FastAPI, configure:

```env
DATABASE_URL=${{Postgres.DATABASE_URL}}
STORAGE_PATH=/app/storage
CORS_ORIGINS=https://SEU-FRONTEND.vercel.app
YOUTUBE_API_KEY=
```

O nome `Postgres` precisa ser igual ao nome do serviço de banco mostrado na Railway. A chave do YouTube é opcional.

### Volume para os vídeos

Adicione um volume ao serviço FastAPI e use exatamente este caminho de montagem:

```text
/app/storage
```

Sem o volume, os vídeos enviados desaparecem quando o serviço é atualizado. Observe também o espaço disponível no plano escolhido: vídeos ocupam bastante armazenamento.

## 3. Criar o frontend na Vercel

1. Na Vercel, selecione **Add New → Project**.
2. Importe o mesmo repositório do GitHub.
3. Configure **Root Directory** como `frontend`.
4. Adicione a variável:

```env
NEXT_PUBLIC_API_URL=https://SEU-BACKEND.up.railway.app
```

5. Publique o projeto.

Depois de conhecer o endereço final da Vercel, volte à Railway e altere `CORS_ORIGINS` para esse endereço exato. Em seguida, faça um novo deploy do backend.

## 4. Conferir

- Abra `https://SEU-BACKEND.up.railway.app/health` e confirme `{"status":"ok"}`.
- Abra `https://SEU-BACKEND.up.railway.app/docs` para conferir o Swagger.
- Abra o endereço da Vercel, crie um projeto de teste e envie arquivos pequenos primeiro.

## Observação sobre custos

Processamento e armazenamento de vídeos consomem CPU, memória, tráfego e espaço em disco. Um volume gratuito pequeno pode comportar apenas um vídeo original. Defina limite de gastos e acompanhe o uso antes de trabalhar com arquivos grandes.
