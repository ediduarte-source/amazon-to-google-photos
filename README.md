# Migrador Amazon Photos → Google Fotos (Álbum "Porta Retrato")

Aplicação local (interface web + linha de comando) para migrar fotos e vídeos exportados do **Amazon Photos** diretamente para o álbum **Porta Retrato** no **Google Fotos**.

## Recursos

- **Download em massa do Amazon Photos de uma vez**: baixa todas as fotos e vídeos da sua biblioteca Amazon Photos em paralelo usando os 3 cookies da sua sessão do navegador (`session-id`, `ubid-acbbr`, `at-acbbr`), com validação MD5, preservação da data da foto, deduplicação e retomada automática.
- **Álbum automático (`Porta Retrato`)**: cria o álbum no Google Fotos na primeira execução e o reutiliza automaticamente em retomadas.
- **Suporte a pastas e arquivos `.zip`**: aponte para uma pasta baixada pelo *Amazon Photos Desktop*, pelo Passo 1 do app ou diretamente para arquivos `.zip` baixados pelo navegador (extração segura incluída).
- **Retomada sem duplicatas**: usa banco SQLite local (`data/migration_state.db`) com hash **SHA-256** de cada arquivo — se a internet cair ou você pausar, basta continuar de onde parou.
- **Upload paralelo + `batchCreate` em lote**: segue as boas práticas oficiais da Google Photos Library API (upload de bytes em paralelo, criação em lotes de até 50 itens e *backoff* automático em caso de erro temporário / limite de taxa).
- **Escopos atualizados da API**: usa `photoslibrary.appendonly` e `photoslibrary.readonly.appcreateddata` (compatível com as mudanças da Google Photos Library API).

---

## Pré-requisitos

- **Python 3.10+** instalado no computador.
- Suas fotos baixadas do Amazon Photos (pelo navegador em `.zip` ou pelo app **Amazon Photos para Desktop**).
  > *Nota:* a Amazon não disponibiliza uma API pública oficial para o Amazon Photos, por isso o método mais seguro e estável é baixar os arquivos/ZIPs e deixar esta aplicação gerenciar toda a organização e envio para o Google Fotos.

---

## Instalação

```bash
cd amazon-to-google-photos
python3 -m venv .venv
source .venv/bin/activate        # Linux / macOS
# .venv\Scripts\activate         # Windows (PowerShell / CMD)

pip install -r requirements.txt
```

---

## Configurar credenciais do Google Fotos (uma única vez)

1. Acesse o [Google Cloud Console](https://console.cloud.google.com/) e crie um projeto (ex: `Migrador Fotos`).
2. Vá em **APIs e Serviços → Biblioteca**, procure por **Photos Library API** e clique em **Ativar**.
3. Vá em **APIs e Serviços → Tela de permissão OAuth**:
   - Escolha **Externo** e preencha nome e e-mail.
   - Em **Usuários de teste**, adicione o e-mail da sua conta Google.
4. Vá em **APIs e Serviços → Credenciais → Criar credenciais → ID do cliente OAuth**:
   - Tipo de aplicativo: **App para computador (Desktop app)**.
   - Baixe o arquivo JSON (`credentials.json`).

---

## Como usar

### Opção 1: Interface Web (recomendado)

```bash
python run.py
```

O navegador abrirá automaticamente em `http://127.0.0.1:8765`:
1. Carregue o arquivo `credentials.json` e clique em **Conectar Conta Google**.
2. Informe o caminho da pasta ou arquivo `.zip` do Amazon Photos.
3. Mantenha o álbum como **Porta Retrato** (ou altere se desejar).
4. Clique em **Analisar Pasta (Prévia)** para conferir a quantidade de arquivos e depois em **Iniciar Migração**.

### Opção 2: Linha de Comando (CLI)

Coloque o arquivo `credentials.json` em `data/credentials.json` e execute:

```bash
# Simulação (apenas conta e indexa os arquivos)
python run.py --cli --source "/caminho/para/AmazonPhotos" --album "Porta Retrato" --dry-run

# Migração real
python run.py --cli --source "/caminho/para/AmazonPhotos" --album "Porta Retrato"

# Reenviar apenas itens que falharam
python run.py --cli --source "/caminho/para/AmazonPhotos" --album "Porta Retrato" --retry-failed
```

---

## Executar os testes automatizados

```bash
pytest -v
```
