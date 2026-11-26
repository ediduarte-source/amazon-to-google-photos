# Migrador Amazon Photos → Google Fotos (Álbum "Porta Retrato")

Aplicação local (interface web + linha de comando) para migrar fotos e vídeos exportados do **Amazon Photos** diretamente para o álbum **Porta Retrato** no **Google Fotos**.

## Recursos

- **Download em massa do Amazon Photos de uma vez, só com e-mail e senha**: faz login na sua conta Amazon (com suporte a verificação em duas etapas) e baixa todas as fotos e vídeos da biblioteca em paralelo, com validação MD5, preservação da data da foto, deduplicação e retomada automática.
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

### Baixar TODAS as fotos do Amazon Photos de uma vez (só e-mail e senha)

Não precisa de credenciais Google nem de copiar cookies — basta o **e-mail e a senha da sua conta Amazon**.

**Pela interface web:** rode `python run.py`, escolha o site (ex: amazon.com.br), digite e-mail e senha no **Passo 1**, clique em **🔐 Entrar na Amazon** e depois em **⬇️ Baixar Todas as Fotos e Vídeos**.

**Pela linha de comando:**

```bash
# Pede e-mail e senha no terminal, faz login e baixa tudo
python run.py --download-amazon --amazon-out ~/AmazonPhotos

# Apenas conta quantos itens existem na biblioteca
python run.py --download-amazon --dry-run

# Entrar com outra conta / sessão expirada
python run.py --download-amazon --amazon-relogin
```

Como o login funciona:
- A aplicação abre um navegador Chromium invisível (Playwright) e faz o login no **formulário oficial da Amazon** com seu e-mail e senha.
- **Verificação em duas etapas:** se a Amazon enviar um código (SMS, e-mail ou app autenticador), a tela (ou o terminal) pede o código.
- **Aprovação pelo celular:** se a Amazon pedir para aprovar o acesso, basta aprovar no celular.
- **Verificação de segurança (CAPTCHA):** a aplicação **não** tenta resolver; ela abre uma janela do navegador para você mesmo concluir. Também é possível marcar *"Mostrar a janela do navegador"* (ou `--show-browser`) desde o início.
- **Sua senha nunca é salva.** Só a sessão de login fica em `data/amazon_session.json` (pasta ignorada pelo git), então nas próximas vezes não é preciso digitar a senha. Clique em **Sair** para apagá-la.

> [!NOTE]
> Na primeira execução, o navegador Chromium (~150 MB) é baixado automaticamente. Para instalar antes: `python -m playwright install chromium`.

O que o downloader faz:
- Lista toda a biblioteca (fotos **e** vídeos) e baixa em paralelo (até 16 simultâneos).
- Valida cada arquivo pelo **MD5** da Amazon e remove duplicatas.
- Preserva a data original da foto no arquivo.
- **Retomada automática**: se cair a internet, você pausar (Ctrl+C) ou a sessão expirar, rode de novo — os arquivos já baixados são pulados.
- Pasta padrão: `data/amazon_downloads/`.

<details>
<summary>Avançado: usar cookies da sessão em vez de e-mail e senha</summary>

1. Faça login em https://www.amazon.com.br/photos no navegador.
2. Pressione **F12** → **Application** → **Cookies** → `https://www.amazon.com.br`.
3. Copie `session-id`, `ubid-acbbr` e `at-acbbr` (em amazon.com: `ubid-main` e `at-main`) para a seção **Avançado** da interface, ou para um `cookies.json`:

```json
{ "session-id": "...", "ubid-acbbr": "...", "at-acbbr": "Atza|..." }
```

```bash
python run.py --download-amazon --amazon-cookies cookies.json
```

Esses cookies dão acesso à sua conta Amazon: não compartilhe e não faça commit do `cookies.json`.
</details>

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
