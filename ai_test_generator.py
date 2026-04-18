"""
AI Test Generator (Open Source Edition) — v0.3.0
Generates production-ready Pytest test cases using OpenAI, Groq, or local Ollama.
Framework-agnostic — works with any Pytest/Playwright project.
"""

import os
import re
import json
import time
import sys
import pathlib
import argparse
import requests
from dotenv import load_dotenv

load_dotenv()

# ── Provider configuration ────────────────────────────────────────────────────

PROVIDERS = {
    "openai": {
        "url": "https://api.openai.com/v1/chat/completions",
        "env_key": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
        "requires_key": True,
    },
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "env_key": "GROQ_API_KEY",
        "default_model": "llama-3.1-8b-instant",
        "requires_key": True,
    },
    "ollama": {
        "url": "http://localhost:11434/v1/chat/completions",
        "url_env_key": "OLLAMA_BASE_URL",   # overrides url if set
        "env_key": None,
        "default_model": "llama3.2",
        "requires_key": False,              # no API key needed for local Ollama
    },
}

SYSTEM_PROMPT = (
    "You are a Senior QA Automation Engineer. "
    "Generate practical, production-ready Python test cases only. "
    "Output only valid Python code — no explanations, no markdown fences. "
    "Use Pytest conventions, Allure decorators, and clean OOP patterns."
)

# ── Security prompt templates ─────────────────────────────────────────────────

SECURITY_PROMPT_API = """
Additionally, generate 3 SECURITY test cases covering OWASP API Top 10 basics:
- Auth bypass: test the endpoint with no Authorization header, an invalid token, and a token from a wrong role. Assert a 401 or 403 is returned.
- Injection: if the endpoint accepts string parameters, test with a SQL injection payload (e.g. ' OR 1=1--) and assert the response is NOT a 500 error.
- Sensitive data exposure: assert that the response body does NOT contain raw passwords, private keys, or unmasked PII.
Name all security tests with the prefix 'test_security_' so they can be run separately.
"""

SECURITY_PROMPT_WEB = """
Additionally, generate 3 SECURITY test cases:
- Session invalidation: clear all cookies, reload the authenticated page, assert a redirect to the login page occurs.
- Source inspection: assert the rendered page source does NOT contain raw passwords or secret tokens.
- XSS resistance: fill any visible text input with '<script>alert(1)</script>' and assert no alert dialog appears.
Name all security tests with the prefix 'test_security_'.
"""

# ── PII patterns ──────────────────────────────────────────────────────────────

_PII_PATTERNS = [
    # Email addresses
    (re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"), "[EMAIL]"),
    # UUIDs (e.g. 550e8400-e29b-41d4-a716-446655440000)
    (re.compile(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    ), "[UUID]"),
    # JWT tokens — three base64url segments separated by dots starting with 'ey'
    (re.compile(
        r"ey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"
    ), "[JWT]"),
    # US SSN
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    # Credit card — 13-16 consecutive digits not adjacent to other digits (reduces ID false positives)
    (re.compile(r"(?<!\d)(\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{1,4})(?!\d)"), "[CC_NUMBER]"),
]


def _mask_pii(text: str) -> str:
    """Replace common PII patterns with safe placeholders before sending to AI."""
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# ── Rich helpers (graceful fallback if rich not installed) ────────────────────

try:
    from rich.console import Console
    from rich.syntax import Syntax
    from rich.panel import Panel
    from rich.table import Table
    _console = Console()
    _RICH = True
except ImportError:
    _RICH = False
    _console = None


def _print_code(code: str) -> None:
    if _RICH:
        _console.print(Panel(
            Syntax(code, "python", theme="monokai", line_numbers=True),
            title="[bold cyan]Generated Test Code[/bold cyan]", expand=True,
        ))
    else:
        print("\n" + "=" * 60)
        print("AI TEST GENERATOR OUTPUT")
        print("=" * 60)
        print(code)


def _print_summary(table_rows: list) -> None:
    if _RICH:
        table = Table(show_header=False, box=None, padding=(0, 2))
        for label, value in table_rows:
            table.add_row(f"[bold]{label}[/bold]", str(value))
        _console.print(Panel(table, title="[bold green]Summary[/bold green]"))
    else:
        for label, value in table_rows:
            print(f"  {label}: {value}")


def _status(msg: str) -> None:
    if _RICH:
        _console.print(f"[dim]{msg}[/dim]")
    else:
        print(msg)


def _warn(msg: str) -> None:
    if _RICH:
        _console.print(f"[bold yellow]WARNING:[/bold yellow] {msg}")
    else:
        print(f"WARNING: {msg}")


def _error(msg: str) -> None:
    if _RICH:
        _console.print(f"[bold red]ERROR:[/bold red] {msg}")
    else:
        print(f"ERROR: {msg}", file=sys.stderr)


# ── Core AI call ──────────────────────────────────────────────────────────────

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3


def _call_ai(
    prompt: str,
    provider: str = "openai",
    model: str = None,
    verbose: bool = False,
    dry_run: bool = False,
    ollama_url: str = None,
) -> str:
    """Send prompt to the selected AI provider and return generated code.

    Retries up to _MAX_RETRIES times with exponential backoff on rate-limit
    and server errors (429, 5xx). Supports OpenAI, Groq, and local Ollama.
    """
    cfg = PROVIDERS.get(provider)
    if not cfg:
        return f"ERROR: Unknown provider '{provider}'. Valid: {', '.join(PROVIDERS)}"

    # Resolve base URL — priority: CLI --ollama-url > env var > hardcoded default
    base_url = cfg["url"]
    if ollama_url:
        base_url = ollama_url.rstrip("/") + "/v1/chat/completions"
    elif cfg.get("url_env_key"):
        env_val = os.getenv(cfg["url_env_key"], "").strip()
        if env_val:
            base_url = env_val.rstrip("/") + "/v1/chat/completions"

    resolved_model = model or cfg["default_model"]

    if verbose or dry_run:
        if _RICH:
            _console.print(Panel(prompt, title="[yellow]Prompt sent to AI[/yellow]"))
        else:
            print(f"\n--- PROMPT ---\n{prompt}\n---")
        _status(f"Provider: {provider} | Model: {resolved_model} | URL: {base_url}")

    if dry_run:
        return "# DRY-RUN: No AI call was made."

    # Resolve API key (skipped in dry-run mode above)
    if cfg.get("requires_key", True):
        api_key = os.getenv(cfg["env_key"], "").replace('"', '').replace("'", "").strip()
        if not api_key:
            return f"ERROR: {cfg['env_key']} not found in environment variables."
    else:
        api_key = "ollama"   # Ollama's OpenAI-compat endpoint accepts any bearer value

    payload = {
        "model": resolved_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 3000,
        "temperature": 0.2,
    }
    req_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            if _RICH:
                with _console.status(
                    f"[cyan]Calling {provider.upper()} ({resolved_model})...[/cyan]"
                ):
                    resp = requests.post(base_url, headers=req_headers, json=payload, timeout=60)
            else:
                _status(f"Calling {provider.upper()} ({resolved_model})...")
                resp = requests.post(base_url, headers=req_headers, json=payload, timeout=60)
        except requests.RequestException as e:
            if attempt == _MAX_RETRIES:
                return f"ERROR: Network error after {_MAX_RETRIES} attempts: {e}"
            wait = 2 ** attempt
            _status(f"Network error — retrying in {wait}s (attempt {attempt}/{_MAX_RETRIES})...")
            time.sleep(wait)
            continue

        if resp.status_code in _RETRYABLE_STATUS:
            if attempt == _MAX_RETRIES:
                return f"API Error {resp.status_code}: {resp.text[:500]}"
            wait = 2 ** attempt
            _status(
                f"HTTP {resp.status_code} — retrying in {wait}s "
                f"(attempt {attempt}/{_MAX_RETRIES})..."
            )
            time.sleep(wait)
            continue

        if not resp.ok:
            return f"API Error {resp.status_code}: {resp.text[:500]}"

        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as e:
            return f"ERROR: Unexpected API response format: {e}. Raw: {resp.text[:300]}"

        if verbose:
            usage = data.get("usage", {})
            _status(
                f"Tokens — prompt: {usage.get('prompt_tokens', '?')} "
                f"| completion: {usage.get('completion_tokens', '?')}"
            )

        return content

    return "ERROR: Exhausted all retries without a successful response."


# ── Prompt helpers ────────────────────────────────────────────────────────────

def _append_custom_rules(prompt: str, custom_rules: str = None) -> str:
    if custom_rules:
        prompt += f"\n\nCRITICAL CUSTOM ARCHITECTURE RULES (MUST follow these):\n{custom_rules}"
    return prompt


def _append_security_rules(prompt: str, mode: str, security: bool) -> str:
    """Append OWASP-basics security test instructions when --security is set."""
    if not security:
        return prompt
    return prompt + (SECURITY_PROMPT_API if mode == "api" else SECURITY_PROMPT_WEB)


def _validate_python(code: str) -> tuple:
    """Returns (is_valid: bool, error_message: str)."""
    try:
        compile(code, "<generated>", "exec")
        return True, ""
    except SyntaxError as e:
        return False, str(e)


def _strip_markdown_fences(code: str) -> str:
    """Remove ```python ... ``` wrappers that some models add."""
    lines = code.strip().splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


# ── Global config discovery ───────────────────────────────────────────────────

def _load_global_config() -> dict:
    """Walk from cwd toward filesystem root looking for .aitestrc or aitest.config.yaml.

    Returns the parsed config dict, or an empty dict if no file is found.
    CLI arguments always override config values.
    """
    try:
        import yaml
    except ImportError:
        return {}

    search_names = [".aitestrc", "aitest.config.yaml"]
    for directory in [pathlib.Path.cwd(), *pathlib.Path.cwd().parents]:
        for name in search_names:
            candidate = directory / name
            if candidate.exists():
                try:
                    with open(candidate, "r", encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                    _status(f"Config loaded from: {candidate}")
                    return data
                except Exception as e:
                    _warn(f"Failed to parse {candidate}: {e}")
                    return {}
    return {}


# ── OpenAPI / Swagger import ──────────────────────────────────────────────────

def _load_openapi(source: str) -> dict:
    """Load an OpenAPI spec from a local file path or a remote URL."""
    try:
        import yaml
    except ImportError:
        _error("PyYAML required for OpenAPI import. Run: pip install pyyaml")
        sys.exit(1)

    if source.startswith(("http://", "https://")):
        _status(f"Fetching OpenAPI spec from: {source}")
        resp = requests.get(source, timeout=15)
        resp.raise_for_status()
        if source.rstrip("?").endswith((".yaml", ".yml")):
            return yaml.safe_load(resp.text)
        try:
            return resp.json()
        except Exception:
            return yaml.safe_load(resp.text)
    else:
        _status(f"Loading OpenAPI spec from: {source}")
        with open(source, "r", encoding="utf-8") as f:
            if source.endswith((".yaml", ".yml")):
                return yaml.safe_load(f)
            return json.load(f)


def _extract_openapi_endpoints(spec: dict) -> list:
    """Extract all endpoint definitions from an OpenAPI 3.x or Swagger 2.x spec.

    Returns a list of dicts: {url, method, summary, parameters, request_body, responses}
    """
    endpoints = []

    # Determine base URL
    base_url = ""
    if "servers" in spec and spec["servers"]:                  # OpenAPI 3.x
        base_url = spec["servers"][0].get("url", "").rstrip("/")
    elif "host" in spec:                                        # Swagger 2.x
        scheme = (spec.get("schemes") or ["https"])[0]
        base_url = f"{scheme}://{spec['host']}{spec.get('basePath', '')}"

    allowed = {"get", "post", "put", "patch", "delete", "head", "options"}
    for path, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in allowed or not isinstance(operation, dict):
                continue
            endpoints.append({
                "url": base_url + path,
                "method": method.upper(),
                "summary": operation.get("summary", ""),
                "parameters": operation.get("parameters", []),
                "request_body": operation.get("requestBody", {}),
                "responses": operation.get("responses", {}),
            })

    return endpoints


def _openapi_endpoint_to_prompt_context(ep: dict) -> str:
    """Convert an endpoint dict to a compact AI-readable context string."""
    lines = [f"Endpoint: {ep['method']} {ep['url']}"]
    if ep.get("summary"):
        lines.append(f"Summary: {ep['summary']}")
    if ep.get("parameters"):
        # Truncate to avoid token overflow on large specs
        params_str = json.dumps(ep["parameters"], indent=2)[:1000]
        lines.append(f"Parameters: {params_str}")
    if ep.get("request_body"):
        body_str = json.dumps(ep["request_body"], indent=2)[:800]
        lines.append(f"Request Body Schema: {body_str}")
    if ep.get("responses"):
        resp_str = json.dumps(ep["responses"], indent=2)[:800]
        lines.append(f"Response Schemas: {resp_str}")
    return "\n".join(lines)


# ── Postman Collection import ─────────────────────────────────────────────────

def _resolve_postman_variables(text: str) -> str:
    """Replace Postman {{VariableName}} placeholders with readable <VariableName> strings."""
    return re.sub(r"\{\{(\w+)\}\}", lambda m: f"<{m.group(1)}>", text)


def _extract_postman_requests(collection: dict) -> list:
    """Recursively flatten a Postman v2.0/v2.1 collection into a list of request dicts.

    Returns list of {url, method, name, headers, body}.
    """
    results = []

    def _walk(items: list) -> None:
        for item in items:
            if "item" in item:                          # folder — recurse
                _walk(item["item"])
            elif "request" in item:
                req = item["request"]
                raw_url = req.get("url", "")
                # url can be a string (v2.0) or an object with a "raw" key (v2.1)
                url = raw_url if isinstance(raw_url, str) else raw_url.get("raw", "")
                url = _resolve_postman_variables(url)

                method = req.get("method", "GET").upper()
                headers = {
                    h["key"]: h["value"]
                    for h in req.get("header", [])
                    if not h.get("disabled")
                }
                body_obj = req.get("body", {}) or {}
                body = body_obj.get("raw", "") if body_obj.get("mode") == "raw" else ""

                results.append({
                    "url": url,
                    "method": method,
                    "name": item.get("name", ""),
                    "headers": headers,
                    "body": body,
                })

    _walk(collection.get("item", []))
    return results


# ── Main generator methods ────────────────────────────────────────────────────

class AITestGenerator:

    @staticmethod
    def generate_web_tests(
        page_source: str,
        page_url: str = "",
        custom_rules: str = None,
        provider: str = "openai",
        model: str = None,
        verbose: bool = False,
        dry_run: bool = False,
        security: bool = False,
        mask_pii: bool = False,
        ollama_url: str = None,
    ) -> str:
        _status(f"Analyzing web page: {page_url}")
        if len(page_source) > 4000:
            _warn(
                f"HTML is {len(page_source):,} chars — truncated to 4,000 for the AI prompt. "
                "Tests will be based on the first portion of the page only."
            )
        truncated = page_source[:4000]
        if mask_pii:
            truncated = _mask_pii(truncated)
            _status("PII masking applied to HTML source.")

        prompt = f"""Analyze this HTML and generate 5 Pytest test cases.
Page URL: {page_url}
HTML Source:
{truncated}

Requirements:
- Use Playwright with Python.
- Implement the Page Object Model (POM) pattern.
- Use 'expect' assertions from playwright.sync_api.
- Add @allure.title and @allure.step decorators to every test.
- Output ONLY valid Python code — no markdown, no explanations."""

        prompt = _append_custom_rules(prompt, custom_rules)
        prompt = _append_security_rules(prompt, "web", security)
        result = _call_ai(
            prompt, provider=provider, model=model,
            verbose=verbose, dry_run=dry_run, ollama_url=ollama_url,
        )
        return _strip_markdown_fences(result)

    @staticmethod
    def generate_api_tests(
        endpoint_url: str,
        method: str = "GET",
        sample_response: dict = None,
        headers: dict = None,
        body: str = None,
        custom_rules: str = None,
        provider: str = "openai",
        model: str = None,
        verbose: bool = False,
        dry_run: bool = False,
        security: bool = False,
        mask_pii: bool = False,
        context: str = None,
        ollama_url: str = None,
    ) -> str:
        """Generate API test cases.

        Args:
            context: Optional pre-formatted context string (used by OpenAPI importer
                     instead of sample_response).
        """
        _status(f"Analyzing API endpoint: [{method}] {endpoint_url}")

        if context:
            endpoint_context = context
        else:
            response_str = json.dumps(sample_response, indent=2) if sample_response else "Not provided"
            if mask_pii:
                response_str = _mask_pii(response_str)
                _status("PII masking applied to sample response.")
            headers_str = json.dumps(headers, indent=2) if headers else "None"
            body_str = body or "Not provided"
            endpoint_context = (
                f"Endpoint: {method} {endpoint_url}\n"
                f"Request Headers: {headers_str}\n"
                f"Request Body: {body_str}\n"
                f"Sample Response: {response_str}"
            )

        prompt = f"""Generate 5 Pytest API test cases for this endpoint.
{endpoint_context}

Requirements:
- Use Python 'requests' library.
- Validate status codes and response JSON structure.
- Include both positive and negative test scenarios (e.g., 200, 400, 401, 404).
- Add response time assertion (under 2000ms).
- Use allure decorators.
- Output ONLY valid Python code — no markdown, no explanations."""

        prompt = _append_custom_rules(prompt, custom_rules)
        prompt = _append_security_rules(prompt, "api", security)
        result = _call_ai(
            prompt, provider=provider, model=model,
            verbose=verbose, dry_run=dry_run, ollama_url=ollama_url,
        )
        return _strip_markdown_fences(result)


# ── GitHub Actions template ───────────────────────────────────────────────────

def _generate_ci_template(test_file: str = "test_generated.py") -> str:
    return f"""name: AI Generated Tests

on:
  push:
    branches: [ main ]
  pull_request:
    branches: [ main ]

jobs:
  test:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          playwright install chromium

      - name: Run tests and collect Allure results
        run: pytest {test_file} --tb=short -v --alluredir=allure-results

      - name: Upload Allure results
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: allure-results
          path: allure-results/
"""


# ── File helpers ──────────────────────────────────────────────────────────────

def _url_to_filename(url: str, method: str = "") -> str:
    slug = re.sub(r"https?://", "", url).replace("/", "_").replace(".", "_").strip("_")
    slug = re.sub(r"[^a-zA-Z0-9_]", "", slug)[:40]
    prefix = f"test_{method.lower()}_" if method else "test_"
    return prefix + slug


def _save_code(code: str, filepath: str) -> None:
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(code)
    _status(f"Saved: {filepath}")


# ── Batch mode ────────────────────────────────────────────────────────────────

_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def _run_batch(
    config_path: str,
    verbose: bool = False,
    dry_run: bool = False,
    ollama_url: str = None,
) -> None:
    try:
        import yaml
    except ImportError:
        _error("PyYAML is required for batch mode. Run: pip install pyyaml")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    provider = cfg.get("provider", "openai")
    model = cfg.get("model", None)
    output_dir = cfg.get("output_dir", "./generated_tests")
    security = cfg.get("security", False)
    mask_pii = cfg.get("pii_masking", False)
    os.makedirs(output_dir, exist_ok=True)

    custom_rules = None
    rules_file = cfg.get("rules_file")
    if rules_file and os.path.exists(rules_file):
        with open(rules_file, "r", encoding="utf-8") as f:
            custom_rules = f.read()

    results = []
    shared = dict(
        custom_rules=custom_rules, provider=provider, model=model,
        verbose=verbose, dry_run=dry_run, security=security,
        mask_pii=mask_pii, ollama_url=ollama_url,
    )

    # ── OpenAPI spec (alternative to api_tests list) ──────────────────────────
    openapi_spec_path = cfg.get("openapi_spec")
    if openapi_spec_path:
        spec = _load_openapi(openapi_spec_path)
        endpoints = _extract_openapi_endpoints(spec)
        _status(f"OpenAPI: found {len(endpoints)} endpoints in spec.")
        for ep in endpoints:
            start = time.time()
            context = _openapi_endpoint_to_prompt_context(ep)
            code = AITestGenerator.generate_api_tests(
                endpoint_url=ep["url"], method=ep["method"],
                context=context, **shared,
            )
            filename = _url_to_filename(ep["url"], ep["method"]) + ".py"
            _save_code(code, os.path.join(output_dir, filename))
            results.append((f"[OpenAPI] {ep['method']} {ep['url']}", filename,
                            f"{round(time.time() - start, 1)}s"))

    # ── Postman collection (alternative to api_tests list) ───────────────────
    postman_path = cfg.get("postman_collection")
    if postman_path:
        with open(postman_path, "r", encoding="utf-8") as f:
            collection = json.load(f)
        postman_reqs = _extract_postman_requests(collection)
        _status(f"Postman: found {len(postman_reqs)} requests in collection.")
        for req in postman_reqs:
            start = time.time()
            code = AITestGenerator.generate_api_tests(
                endpoint_url=req["url"], method=req["method"],
                headers=req.get("headers"), body=req.get("body"), **shared,
            )
            filename = _url_to_filename(req["url"], req["method"]) + ".py"
            _save_code(code, os.path.join(output_dir, filename))
            results.append((f"[Postman] {req['method']} {req['url']}", filename,
                            f"{round(time.time() - start, 1)}s"))

    # ── Explicit API tests list ───────────────────────────────────────────────
    for entry in cfg.get("api_tests", []):
        url = entry.get("url", "").strip()
        if not url.startswith(("http://", "https://")):
            _error(f"Skipping invalid URL in batch config: {url!r}")
            continue
        method = entry.get("method", "GET").upper()
        if method not in _ALLOWED_METHODS:
            _error(f"Skipping unsupported HTTP method '{method}' for {url}")
            continue

        body = entry.get("body")
        start = time.time()

        req_headers: dict = {}
        if entry.get("auth_token"):
            req_headers["Authorization"] = f"Bearer {entry['auth_token']}"
        for raw_header in entry.get("headers", []):
            if ": " in raw_header:
                k, v = raw_header.split(": ", 1)
                req_headers[k.strip()] = v.strip()

        sample = None
        try:
            resp = requests.request(method, url, headers=req_headers or None,
                                    data=body, timeout=15)
            sample = resp.json() if resp.ok else None
        except Exception:
            pass

        code = AITestGenerator.generate_api_tests(
            endpoint_url=url, method=method, sample_response=sample,
            headers=req_headers or None, body=body, **shared,
        )
        filename = _url_to_filename(url, method) + ".py"
        _save_code(code, os.path.join(output_dir, filename))
        results.append((f"[API] {method} {url}", filename,
                        f"{round(time.time() - start, 1)}s"))

    # ── Web tests list ────────────────────────────────────────────────────────
    for entry in cfg.get("web_tests", []):
        url = entry["url"]
        start = time.time()
        source = ""
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch()
                pg = browser.new_page()
                pg.goto(url)
                source = pg.content()
                browser.close()
        except Exception as e:
            _error(f"Could not fetch {url}: {e}")
            continue

        code = AITestGenerator.generate_web_tests(
            page_source=source, page_url=url, **shared,
        )
        filename = _url_to_filename(url) + ".py"
        _save_code(code, os.path.join(output_dir, filename))
        results.append((f"[WEB] {url}", filename,
                        f"{round(time.time() - start, 1)}s"))

    # ── Results table ─────────────────────────────────────────────────────────
    if _RICH:
        table = Table(title="Batch Results", show_lines=True)
        table.add_column("Target", style="cyan")
        table.add_column("Output File", style="green")
        table.add_column("Time", style="yellow")
        for row in results:
            table.add_row(*row)
        _console.print(table)
    else:
        for row in results:
            print(f"  {row[0]} → {row[1]} ({row[2]})")


# ── CLI / main ────────────────────────────────────────────────────────────────

def main() -> None:
    # Load project-level config first (.aitestrc / aitest.config.yaml)
    global_cfg = _load_global_config()

    parser = argparse.ArgumentParser(
        description="AI Test Generator — Generate Pytest tests instantly from any URL or API endpoint.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python ai_test_generator.py --api-url https://jsonplaceholder.typicode.com/posts/1
  python ai_test_generator.py --web-url https://example.com --provider openai --output test_example.py
  python ai_test_generator.py --api-url https://api.example.com/me --auth-token mytoken
  python ai_test_generator.py --provider ollama --model llama3.2 --api-url https://api.example.com/users
  python ai_test_generator.py --openapi https://petstore3.swagger.io/api/v3/openapi.json
  python ai_test_generator.py --postman MyCollection.postman_collection.json
  python ai_test_generator.py --batch batch_config.yaml
  python ai_test_generator.py --web-url https://example.com --dry-run --verbose
        """,
    )

    # Target
    parser.add_argument("--web-url", help="Generate web tests from a URL")
    parser.add_argument("--api-url", help="Generate API tests from an endpoint")
    parser.add_argument("--batch", metavar="CONFIG.yaml", help="Run batch mode from a YAML config file")
    parser.add_argument("--openapi", metavar="PATH_OR_URL",
                        help="Generate tests from an OpenAPI/Swagger spec (file path or URL)")
    parser.add_argument("--postman", metavar="PATH",
                        help="Generate tests from a Postman collection JSON file")

    # AI provider — default=None so global config can fill in before hardcoded fallback
    parser.add_argument("--provider", default=None,
                        help="AI provider: openai, groq, or ollama (default: openai)")
    parser.add_argument("--model", default=None,
                        help="Override the default model for the selected provider")
    parser.add_argument("--ollama-url", default=None,
                        help="Ollama base URL (e.g. http://gpu-server:11434). "
                             "Overrides OLLAMA_BASE_URL env var.")

    # API options
    parser.add_argument("--method", default="GET", help="HTTP method for API tests (default: GET)")
    parser.add_argument("--auth-token", help="Bearer token for authenticated API requests")
    parser.add_argument("--header", action="append", metavar="Key: Value",
                        help="Custom request header (repeatable)")
    parser.add_argument("--body", help="Request body JSON string for POST/PUT tests")

    # Output
    parser.add_argument("--output", help="Save generated code to file (e.g., test_login.py)")
    parser.add_argument("--generate-ci", action="store_true",
                        help="Generate a .github/workflows/ai_generated_tests.yml file")

    # Rules
    parser.add_argument("--rules", help="Inline custom architecture rules")
    parser.add_argument("--rules-file", help="Path to a .txt file with custom architecture rules")

    # Enhancements
    parser.add_argument("--security", action="store_true",
                        help="Also generate OWASP-basics security test cases")
    parser.add_argument("--mask-pii", action="store_true",
                        help="Mask emails, UUIDs, JWTs, and SSNs before sending to AI")

    # Debug
    parser.add_argument("--verbose", action="store_true", help="Show the full prompt sent to AI")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the AI prompt without making any API call")

    args = parser.parse_args()

    # ── Apply global config as fallback (CLI always wins) ─────────────────────
    args.provider = args.provider or global_cfg.get("provider", "openai")
    args.model = args.model or global_cfg.get("model") or None
    args.rules_file = args.rules_file or global_cfg.get("rules_file") or None
    if not args.security and global_cfg.get("security"):
        args.security = True
    if not args.mask_pii and global_cfg.get("pii_masking"):
        args.mask_pii = True
    if not args.verbose and global_cfg.get("verbose"):
        args.verbose = True

    # Validate provider
    valid_providers = list(PROVIDERS.keys())
    if args.provider not in valid_providers:
        _error(f"Unknown provider '{args.provider}'. Valid: {', '.join(valid_providers)}")
        sys.exit(1)

    # Ollama URL shorthand
    if args.ollama_url:
        os.environ["OLLAMA_BASE_URL"] = args.ollama_url

    # ── Batch mode ─────────────────────────────────────────────────────────────
    if args.batch:
        _run_batch(args.batch, verbose=args.verbose, dry_run=args.dry_run,
                   ollama_url=args.ollama_url)
        return

    # ── Build custom rules ─────────────────────────────────────────────────────
    custom_rules = args.rules
    if args.rules_file and os.path.exists(args.rules_file):
        with open(args.rules_file, "r", encoding="utf-8") as f:
            custom_rules = f.read()

    # ── Build request headers ──────────────────────────────────────────────────
    req_headers: dict = {}
    if args.auth_token:
        req_headers["Authorization"] = f"Bearer {args.auth_token}"
    for h in (args.header or []):
        if ": " in h:
            k, v = h.split(": ", 1)
            req_headers[k.strip()] = v.strip()

    shared = dict(
        custom_rules=custom_rules,
        provider=args.provider,
        model=args.model,
        verbose=args.verbose,
        dry_run=args.dry_run,
        security=args.security,
        mask_pii=args.mask_pii,
        ollama_url=args.ollama_url,
    )

    start = time.time()
    result = ""
    is_valid = True

    # ── OpenAPI import ─────────────────────────────────────────────────────────
    if args.openapi:
        spec = _load_openapi(args.openapi)
        endpoints = _extract_openapi_endpoints(spec)
        if not endpoints:
            _error("No endpoints found in the OpenAPI spec.")
            sys.exit(1)
        _status(f"Found {len(endpoints)} endpoints. Generating tests...")

        output_dir = args.output or "./generated_tests"
        if not args.output or not args.output.endswith(".py"):
            os.makedirs(output_dir, exist_ok=True)

        results_table = []
        for ep in endpoints:
            ep_start = time.time()
            context = _openapi_endpoint_to_prompt_context(ep)
            code = AITestGenerator.generate_api_tests(
                endpoint_url=ep["url"], method=ep["method"], context=context, **shared,
            )
            if args.output and args.output.endswith(".py") and len(endpoints) == 1:
                _save_code(code, args.output)
                results_table.append((f"{ep['method']} {ep['url']}", args.output,
                                      f"{round(time.time() - ep_start, 1)}s"))
            else:
                filename = _url_to_filename(ep["url"], ep["method"]) + ".py"
                filepath = os.path.join(output_dir, filename)
                _save_code(code, filepath)
                results_table.append((f"{ep['method']} {ep['url']}", filepath,
                                      f"{round(time.time() - ep_start, 1)}s"))

        if _RICH:
            table = Table(title="OpenAPI Results", show_lines=True)
            table.add_column("Endpoint", style="cyan")
            table.add_column("File", style="green")
            table.add_column("Time", style="yellow")
            for row in results_table:
                table.add_row(*row)
            _console.print(table)
        else:
            for row in results_table:
                print(f"  {row[0]} → {row[1]} ({row[2]})")
        return

    # ── Postman import ─────────────────────────────────────────────────────────
    if args.postman:
        with open(args.postman, "r", encoding="utf-8") as f:
            collection = json.load(f)
        postman_reqs = _extract_postman_requests(collection)
        if not postman_reqs:
            _error("No requests found in the Postman collection.")
            sys.exit(1)
        _status(f"Found {len(postman_reqs)} requests. Generating tests...")

        output_dir = args.output or "./generated_tests"
        os.makedirs(output_dir, exist_ok=True)
        results_table = []
        for req in postman_reqs:
            req_start = time.time()
            code = AITestGenerator.generate_api_tests(
                endpoint_url=req["url"], method=req["method"],
                headers=req.get("headers"), body=req.get("body"), **shared,
            )
            filename = _url_to_filename(req["url"], req["method"]) + ".py"
            filepath = os.path.join(output_dir, filename)
            _save_code(code, filepath)
            results_table.append((f"{req['method']} {req['url']}", filename,
                                  f"{round(time.time() - req_start, 1)}s"))

        if _RICH:
            table = Table(title="Postman Results", show_lines=True)
            table.add_column("Request", style="cyan")
            table.add_column("File", style="green")
            table.add_column("Time", style="yellow")
            for row in results_table:
                table.add_row(*row)
            _console.print(table)
        else:
            for row in results_table:
                print(f"  {row[0]} → {row[1]} ({row[2]})")
        return

    # ── Single mode ────────────────────────────────────────────────────────────
    if not args.web_url and not args.api_url:
        parser.print_help()
        return

    if args.web_url:
        try:
            from playwright.sync_api import sync_playwright
            _status(f"Fetching page: {args.web_url}")
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                page.goto(args.web_url, timeout=30000)
                source = page.content()
                browser.close()
            result = AITestGenerator.generate_web_tests(
                page_source=source, page_url=args.web_url, **shared,
            )
        except Exception as e:
            _error(f"Failed to fetch page: {e}")
            sys.exit(1)

    elif args.api_url:
        sample = None
        try:
            _status(f"Fetching sample response from: {args.api_url}")
            resp = requests.request(
                args.method.upper(), args.api_url,
                headers=req_headers or None, data=args.body, timeout=15,
            )
            sample = resp.json() if resp.ok else None
        except Exception:
            pass

        result = AITestGenerator.generate_api_tests(
            endpoint_url=args.api_url,
            method=args.method.upper(),
            sample_response=sample,
            headers=req_headers if req_headers else None,
            body=args.body,
            **shared,
        )

    elapsed = round(time.time() - start, 1)

    # ── Syntax validation + auto-fix ───────────────────────────────────────────
    is_valid, err = _validate_python(result)
    if not is_valid and not args.dry_run:
        _warn("Syntax error detected — requesting fix from AI...")
        fix_prompt = (
            f"The following Python code has a syntax error: {err}\n"
            f"Fix it and return ONLY the corrected Python code:\n\n{result}"
        )
        result = _strip_markdown_fences(
            _call_ai(fix_prompt, provider=args.provider, model=args.model,
                     ollama_url=args.ollama_url)
        )

    # ── Display ────────────────────────────────────────────────────────────────
    _print_code(result)
    summary = [
        ("Provider", f"{args.provider} / {args.model or PROVIDERS[args.provider]['default_model']}"),
        ("Time", f"{elapsed}s"),
    ]
    if args.security:
        summary.append(("Security tests", "included (OWASP basics)"))
    if args.mask_pii:
        summary.append(("PII masking", "enabled"))
    if args.output:
        summary.append(("Saved to", args.output))
    if not is_valid and not args.dry_run:
        summary.append(("Syntax fix", "applied automatically"))
    _print_summary(summary)

    # ── Save + CI template ─────────────────────────────────────────────────────
    if args.output and not args.dry_run:
        _save_code(result, args.output)

    if args.generate_ci and not args.dry_run:
        ci_dir = ".github/workflows"
        os.makedirs(ci_dir, exist_ok=True)
        ci_path = os.path.join(ci_dir, "ai_generated_tests.yml")
        with open(ci_path, "w", encoding="utf-8") as f:
            f.write(_generate_ci_template(args.output or "test_generated.py"))
        _status(f"CI workflow saved: {ci_path}")


if __name__ == "__main__":
    main()
