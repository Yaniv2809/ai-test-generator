# AI Test Generator

![Python](https://img.shields.io/badge/Python-3.9%2B-blue?logo=python)
![Version](https://img.shields.io/badge/version-0.3.0-informational)
![License](https://img.shields.io/badge/License-MIT-green)
![Providers](https://img.shields.io/badge/AI-OpenAI%20%7C%20Groq%20%7C%20Ollama-blueviolet)
![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)

> **Point it at a URL, an OpenAPI spec, or a Postman collection. Get production-ready Pytest tests in seconds.**

AI Test Generator analyzes web pages and API endpoints using AI and writes complete, executable Pytest test files — with Playwright, Allure decorators, positive & negative scenarios, OWASP security tests, and your team's own architecture rules baked in.

Works fully **offline** with local Ollama models — no data ever leaves your network.

---

## Quick Start (3 steps)

```bash
# 1. Install
pip install -r requirements.txt && playwright install chromium

# 2. Set your API key
cp .env.example .env   # add your OPENAI_API_KEY, GROQ_API_KEY, or configure Ollama

# 3. Generate tests
python ai_test_generator.py --api-url https://jsonplaceholder.typicode.com/posts/1
```

---

## Features

| Feature | Description |
|---|---|
| **3 AI Providers** | OpenAI, Groq (free), or local Ollama — fully offline |
| **Web Tests** | Playwright + Page Object Model generated from any live URL |
| **API Tests** | REST endpoint analysis with positive, negative, and timing assertions |
| **OpenAPI Import** | `--openapi swagger.json` — generate tests for an entire API at once |
| **Postman Import** | `--postman collection.json` — convert any Postman collection to Pytest |
| **Security Tests** | `--security` adds OWASP API/Web basics to every generation |
| **PII Masking** | `--mask-pii` strips emails, UUIDs, JWTs, SSNs before sending to AI |
| **Batch Mode** | YAML config to test entire systems in one command |
| **Auth Support** | Bearer tokens and custom headers injected automatically |
| **Global Config** | `.aitestrc` in project root — no need to repeat flags every run |
| **Custom Rules** | Inject your team's framework conventions into every generation |
| **Rich Output** | Syntax-highlighted code, spinner, summary table |
| **Syntax Validation** | Auto-detects and fixes broken Python before saving |
| **CI Template** | `--generate-ci` creates a ready GitHub Actions workflow |
| **pip installable** | Works as `ai-test-gen` CLI or importable Python module |

---

## Installation

```bash
git clone https://github.com/your-username/ai-test-generator.git
cd ai-test-generator
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # configure your API key(s)
```

---

## AI Providers

### OpenAI (default)
```bash
# In .env:
OPENAI_API_KEY=sk-...

python ai_test_generator.py --provider openai --api-url https://api.example.com/users
```

### Groq (free tier)
```bash
# In .env:
GROQ_API_KEY=gsk_...

python ai_test_generator.py --provider groq --model llama-3.3-70b-versatile \
  --api-url https://api.example.com/users
```

### Ollama (local / air-gapped) 🔒
Run everything locally — no data sent to external APIs. Ideal for regulated industries.

```bash
# 1. Install Ollama: https://ollama.com/
# 2. Pull a model
ollama pull llama3.2

# 3. Run tests — zero config needed if Ollama is on localhost
python ai_test_generator.py --provider ollama --api-url https://api.example.com/users

# Running Ollama on a different host?
python ai_test_generator.py --provider ollama --ollama-url http://gpu-server:11434 \
  --api-url https://api.example.com/users
```

---

## Input Methods

### Single URL
```bash
# API endpoint
python ai_test_generator.py --api-url https://api.example.com/posts

# Web page
python ai_test_generator.py --web-url https://app.example.com/login
```

### OpenAPI / Swagger Spec
Generates tests for **every endpoint** in the spec at once.

```bash
# From a local file
python ai_test_generator.py --openapi ./swagger.json --output ./generated_tests/

# From a URL
python ai_test_generator.py --openapi https://petstore3.swagger.io/api/v3/openapi.json

# Supports OpenAPI 3.x and Swagger 2.x, JSON and YAML
```

### Postman Collection
Converts a Postman v2.0/v2.1 collection to Pytest test files.

```bash
python ai_test_generator.py --postman ./MyAPI.postman_collection.json
```

### Batch Mode (YAML)
```bash
python ai_test_generator.py --batch batch_config.yaml
```

```yaml
# batch_config.yaml
provider: openai
model: gpt-4o-mini
output_dir: ./generated_tests/
rules_file: my_framework_rules.txt
security: true
pii_masking: true

openapi_spec: ./swagger.json    # Option A: generate from spec
# postman_collection: ./API.json  # Option B: generate from Postman

api_tests:                       # Option C: explicit list
  - url: https://api.example.com/users
    method: GET
  - url: https://api.example.com/login
    method: POST
    body: '{"username": "test", "password": "pass"}'
    auth_token: "mytoken"

web_tests:
  - url: https://app.example.com/dashboard
```

---

## Security Testing

Add `--security` to generate OWASP-basics security tests alongside functional tests.

```bash
python ai_test_generator.py --api-url https://api.example.com/users --security
```

Generated security tests include:
- **Auth bypass** — no token, invalid token, wrong role token → assert 401/403
- **Injection** — SQL injection payloads in string params → assert no 500
- **Data exposure** — assert response doesn't leak passwords or PII
- **Session invalidation** (web) — assert redirect to login after cookie clear
- **XSS resistance** (web) — `<script>` in inputs, no alert should fire

Security tests are prefixed `test_security_` so they can be run separately:
```bash
pytest test_users.py -k "security"    # security only
pytest test_users.py -k "not security" # functional only
```

---

## PII Masking

For regulated environments (GDPR, HIPAA), use `--mask-pii` to sanitize HTML/JSON before sending to any external AI API.

```bash
python ai_test_generator.py --api-url https://api.example.com/users/1 --mask-pii
```

Detected and replaced with placeholders:

| Pattern | Replacement |
|---|---|
| Email addresses | `[EMAIL]` |
| UUIDs | `[UUID]` |
| JWT tokens | `[JWT]` |
| US Social Security Numbers | `[SSN]` |
| Credit card numbers | `[CC_NUMBER]` |

Enable globally via `.aitestrc`:
```yaml
pii_masking: true
```

---

## Global Config File (`.aitestrc`)

Place a `.aitestrc` file in your project root. The tool discovers it automatically by walking up from the current directory — no flag needed.

```yaml
# .aitestrc — commit this to git to share defaults with your team
provider: groq
model: llama-3.3-70b-versatile
rules_file: ./my_framework_rules.txt
security: false
pii_masking: true
```

CLI arguments always override `.aitestrc` values.

Also supported filename: `aitest.config.yaml`

---

## Custom Architecture Rules

```bash
python ai_test_generator.py --web-url https://app.example.com --rules-file my_framework_rules.txt
```

```
# my_framework_rules.txt
1. Use 'SafeVerifications.check_text()' instead of native Playwright 'expect'.
2. Do not use standard Pytest fixtures — use 'init_browser_session'.
3. Always add @allure.severity(allure.severity_level.NORMAL).
```

---

## All CLI Options

```
Target:
  --web-url URL           Generate web tests from a live URL
  --api-url URL           Generate API tests from an endpoint
  --openapi PATH/URL      Generate tests from an OpenAPI/Swagger spec
  --postman PATH          Generate tests from a Postman collection JSON
  --batch CONFIG.yaml     Run batch mode from a YAML config

AI Provider:
  --provider PROVIDER     openai (default) | groq | ollama
  --model MODEL_ID        Override default model
  --ollama-url URL        Ollama base URL (overrides OLLAMA_BASE_URL)

API Options:
  --method METHOD         HTTP method: GET, POST, PUT, DELETE (default: GET)
  --auth-token TOKEN      Adds Authorization: Bearer <TOKEN>
  --header "Key: Value"   Custom header (repeatable)
  --body '{"k": "v"}'    Request body for POST/PUT

Output:
  --output FILE           Save generated code to file
  --generate-ci           Create .github/workflows/ai_generated_tests.yml

Rules:
  --rules TEXT            Inline custom architecture rules
  --rules-file PATH       Path to .txt file with custom rules

Enhancements:
  --security              Add OWASP security tests to the generation
  --mask-pii              Mask PII before sending HTML/JSON to AI

Debug:
  --verbose               Show full prompt sent to AI + token usage
  --dry-run               Print the prompt without calling AI
```

---

## Using as a Python Module

```python
from ai_test_generator import AITestGenerator

# API tests with security + PII masking
tests = AITestGenerator.generate_api_tests(
    endpoint_url="https://api.example.com/users",
    method="POST",
    sample_response={"id": 1, "name": "John", "email": "john@example.com"},
    headers={"Authorization": "Bearer token123"},
    body='{"name": "John"}',
    provider="ollama",          # runs locally
    model="llama3.2",
    security=True,
    mask_pii=True,
)
print(tests)

# OpenAPI import
from ai_test_generator import _load_openapi, _extract_openapi_endpoints, AITestGenerator

spec = _load_openapi("./swagger.json")
for ep in _extract_openapi_endpoints(spec):
    tests = AITestGenerator.generate_api_tests(
        endpoint_url=ep["url"],
        method=ep["method"],
        context=...,  # from _openapi_endpoint_to_prompt_context(ep)
    )
```

---

## Project Structure

```
ai-test-generator/
├── ai_test_generator.py       # Core tool — all features in one file
├── .aitestrc                  # Project-level config (edit and commit)
├── batch_config.yaml          # Example batch config
├── my_framework_rules.txt     # Example custom rules
├── requirements.txt
├── pyproject.toml             # pip packaging (v0.3.0)
├── .env.example               # API key template
├── .gitignore
└── README.md
```

---

## Origin

Built from production AI test generation logic in the **Financial Integrity Ecosystem** project. Extracted and open-sourced so any QA Engineer can use it in any project — with or without an internet connection.

---

## Roadmap

- [ ] `--heal` mode — fix broken Playwright selectors automatically
- [ ] Stateful flow testing — multi-step YAML scenarios with token passing
- [ ] GraphQL endpoint support
- [ ] Package on PyPI (`pip install ai-test-generator`)

---

## Contributing

PRs welcome. See open issues for ideas.

---

## License

MIT
