# ClipFinder

ClipFinder organiza um vídeo original, referências de cortes e os clipes encontrados no vídeo principal. O fluxo atual usa comparação de áudio entre arquivos locais para localizar uma referência e gerar um novo vídeo recortado.

## O que funciona agora

- Criar e listar projetos.
- Excluir projetos e todos os arquivos associados com confirmação no dashboard.
- Enviar o vídeo original do projeto.
- Enviar arquivos de vídeo que servem como referências de corte.
- Buscar vídeos no YouTube por palavra-chave e salvar seus links como referência do projeto.
- Comparar o áudio das referências enviadas com o vídeo original.
- Gerar e baixar os cortes que forem encontrados.
- Visualizar e testar a API pelo Swagger.

> A busca do YouTube encontra e registra links. Para gerar um corte, envie também o arquivo de vídeo de referência. O aplicativo não baixa vídeos de terceiros.

## Tecnologias

- Frontend: Next.js, TypeScript e Tailwind CSS
- Backend: Python, FastAPI, SQLAlchemy e Alembic
- Banco: PostgreSQL
- Infraestrutura: Redis, Docker e Docker Compose

## Estrutura

```text
clipfinder/
├── frontend/       # Interface Next.js
├── backend/        # API FastAPI e migrações Alembic
├── storage/        # Vídeos enviados e cortes gerados
├── docs/           # Documentação futura
├── docker-compose.yml
├── setup-local.ps1 # Preparação única sem Docker
├── start-local.ps1 # Inicialização diária sem reinstalar nada
└── README.md
```

## Rodar sem Docker (recomendado no seu computador)

Você precisa ter instalado uma vez: Python 3.12 ou superior, Node.js LTS e PostgreSQL.

Como esta versão adiciona novas tabelas e dependências, feche as janelas antigas da API e da interface e rode uma única vez:

```powershell
cd C:\Users\Lenovo\Downloads\Clipfinder
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\setup-local.ps1
```

O script atualiza somente o que estiver faltando, prepara o banco e abre duas janelas: backend e frontend.

Nas próximas vezes, na pasta do projeto, basta executar:

```powershell
.\start-local.ps1
```

Não é necessário executar comandos dentro de `frontend` nem ajustar o Node.js manualmente. O script já corrige o caminho do Node.js.

## Acessos

| Serviço | Endereço |
| --- | --- |
| Interface | [http://localhost:3000](http://localhost:3000) |
| API | [http://localhost:8000](http://localhost:8000) |
| Swagger | [http://localhost:8000/docs](http://localhost:8000/docs) |
| Saúde da API | [http://localhost:8000/health](http://localhost:8000/health) |

## Como usar

1. Abra a interface e crie um projeto.
2. Envie o vídeo original.
3. Em **Vídeos de referência**, envie um ou mais arquivos de cortes conhecidos. Eles devem vir do mesmo vídeo original e conservar o áudio; quanto mais limpo o áudio, melhor o resultado.
4. Clique em **Gerar cortes encontrados**.
5. Atualize o status e baixe cada corte concluído.

Para guardar links encontrados no YouTube, use a busca da tela do projeto e clique em **Usar como referência**. Isso não faz download nem matching automático daquele link: o arquivo de referência continua necessário para a comparação.

## Configurar busca no YouTube (opcional)

Para habilitar a busca do YouTube, crie uma chave da API YouTube Data API v3 no Google Cloud e acrescente esta linha ao final de `backend\.env`:

```env
YOUTUBE_API_KEY=sua_chave_aqui
```

Depois feche as duas janelas abertas e execute ` .\start-local.ps1` novamente. Sem a chave, o restante do sistema continua funcionando normalmente, mas a busca do YouTube informa que falta a configuração.

## API disponível

| Método | Rota | Finalidade |
| --- | --- | --- |
| `POST` | `/projects` | Cria um projeto |
| `GET` | `/projects` | Lista projetos |
| `GET` | `/projects/{id}` | Obtém os dados, referências e cortes do projeto |
| `DELETE` | `/projects/{id}` | Exclui o projeto e seus arquivos |
| `POST` | `/projects/{id}/upload` | Envia o vídeo original |
| `POST` | `/projects/{id}/reference-videos/upload` | Envia um arquivo de referência |
| `GET` | `/projects/{id}/youtube/search` | Busca vídeos no YouTube |
| `POST` | `/projects/{id}/reference-videos/youtube` | Salva um link do YouTube como referência |
| `POST` | `/projects/{id}/generated-clips` | Inicia a geração de cortes |
| `GET` | `/projects/{id}/generated-clips/{clip_id}/download` | Baixa um corte concluído |

## Docker

Em um computador com Docker Desktop funcionando, copie `.env.example` para `.env` e execute:

```powershell
docker compose up --build
```

## Publicar sem Docker

O passo a passo para publicar o frontend na Vercel e o backend, PostgreSQL e armazenamento na Railway está em [`docs/DEPLOYMENT_WITHOUT_DOCKER.md`](docs/DEPLOYMENT_WITHOUT_DOCKER.md).

## Limitações atuais

Ainda não há Whisper, transcrição, IA semântica, TikTok, Celery ou download de vídeos de plataformas. O matching atual é intencionalmente simples e local: usa a semelhança do áudio para encontrar referências que pertencem ao vídeo original.
