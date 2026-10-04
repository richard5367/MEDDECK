import json
import os
import re
import secrets
import sqlite3
import tempfile
import urllib.error
import urllib.request
from collections import defaultdict
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
HP_ID = re.compile(r"^HP:\d{7}$")
ROLES = {"patient": "Patient", "doctor": "Doctor", "researcher": "Researcher"}
SEX_OPTIONS = {"female": "Female", "male": "Male", "intersex": "Intersex", "prefer_not": "Prefer not to say"}

SOURCES = [
    ("HPO", "Phenotypes and disease-phenotype relationships", "https://hpo.jax.org/"),
    ("Orphanet", "Rare disease information and catalogue", "https://www.orpha.net/"),
    ("OMIM", "Hereditary genes and phenotypes; access subject to their terms", "https://www.omim.org/"),
    ("PubMed", "Biomedical literature; queries open NCBI results", "https://pubmed.ncbi.nlm.nih.gov/"),
    ("MONDO", "Disease identifiers", "https://mondo.monarchinitiative.org/"),
    ("ClinVar", "Genetic variants and clinical evidence", "https://www.ncbi.nlm.nih.gov/clinvar/"),
    ("ClinicalTrials.gov", "Registered clinical trials", "https://clinicaltrials.gov/"),
    ("NORD", "Rare disease resources and organizations", "https://rarediseases.org/"),
    ("Global Genes", "Community resources", "https://globalgenes.org/"),
    ("EURORDIS", "European organizations and resources", "https://www.eurordis.org/"),
    ("Rare Disease UK", "Support and information", "https://www.rarediseaseuk.org/"),
    ("Genetic Alliance", "Genetics resources", "https://geneticalliance.org.uk/"),
    ("NIH RePORTER", "NIH-funded research projects", "https://reporter.nih.gov/"),
]


def load_dotenv_file(path=None, override=False):
    """Load key-value pairs from .env file into os.environ."""
    if path is None:
        path = BASE_DIR / ".env"
    path = Path(path)
    if not path.is_file():
        return
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k and v:
                    if override or k not in os.environ or not os.environ[k]:
                        os.environ[k] = v
    except Exception:
        pass


load_dotenv_file()


def _is_writable_dir(path):
    """Return True when the directory exists (or can be created) and accepts writes."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".meddeck-write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _resolve_data_dir():
    """Prefer ./instance, but fall back to a writable dir when running on a read-only filesystem.

    Serverless hosts such as Vercel ship the project as read-only and only allow writes
    under the system temp directory, so the database and secret file have to live elsewhere.
    """
    configured = os.environ.get("MEDDECK_DATA_DIR")
    if configured:
        candidate = Path(configured)
        if _is_writable_dir(candidate):
            return candidate
    instance_dir = BASE_DIR / "instance"
    if _is_writable_dir(instance_dir):
        return instance_dir
    fallback = Path(tempfile.gettempdir()) / "meddeck"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


DATA_DIR = _resolve_data_dir()
DEFAULT_DB = DATA_DIR / "meddeck.sqlite"
DEFAULT_SECRET_FILE = DATA_DIR / "flask-secret.key"


def load_or_create_secret(path):
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        pass
    except OSError:
        # No persistent store available: fall back to an ephemeral secret for this process.
        return secrets.token_urlsafe(48)
    secret = secrets.token_urlsafe(48)
    try:
        with path.open("x", encoding="utf-8") as secret_file:
            secret_file.write(secret)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return secret
    return secret


def connect_db(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def database(path):
    connection = connect_db(path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with database(path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                age INTEGER NOT NULL,
                sex TEXT NOT NULL,
                role TEXT NOT NULL,
                diagnosed INTEGER NOT NULL DEFAULT 0,
                condition TEXT NOT NULL DEFAULT '',
                symptoms TEXT NOT NULL DEFAULT '',
                genes TEXT NOT NULL DEFAULT '',
                hpo_json TEXT NOT NULL DEFAULT '[]',
                consent_ai INTEGER NOT NULL DEFAULT 0,
                gene_candidates_json TEXT NOT NULL DEFAULT '[]',
                consent_gene_graph INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS communities (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                basis TEXT NOT NULL,
                UNIQUE(name COLLATE NOCASE, basis)
            );
            CREATE TABLE IF NOT EXISTS memberships (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
                PRIMARY KEY(user_id, community_id)
            );
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY,
                community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
        if "gene_candidates_json" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN gene_candidates_json TEXT NOT NULL DEFAULT '[]'")
        if "consent_gene_graph" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN consent_gene_graph INTEGER NOT NULL DEFAULT 0")


def load_hpo_data(hpoa_path=None, obo_path=None, gene_path=None):
    """Load disease, term, and gene associations from configured or downloaded HPO files."""
    downloads = Path.home() / "Downloads"
    hpoa_path = hpoa_path or os.environ.get("HPOA_PATH")
    obo_path = obo_path or os.environ.get("HPO_OBO_PATH")
    gene_path = gene_path or os.environ.get("HPO_GENE_PATH")
    if not hpoa_path and (downloads / "phenotype.hpoa").is_file():
        hpoa_path = downloads / "phenotype.hpoa"
    if not obo_path and (downloads / "hp.obo").is_file():
        obo_path = downloads / "hp.obo"
    if not gene_path and (downloads / "genes_to_phenotype.txt").is_file():
        gene_path = downloads / "genes_to_phenotype.txt"
    hpoa_path = Path(hpoa_path) if hpoa_path and Path(hpoa_path).is_file() else None
    obo_path = Path(obo_path) if obo_path and Path(obo_path).is_file() else None
    gene_path = Path(gene_path) if gene_path and Path(gene_path).is_file() else None
    hpoa_mtime = hpoa_path.stat().st_mtime_ns if hpoa_path else 0
    obo_mtime = obo_path.stat().st_mtime_ns if obo_path else 0
    gene_mtime = gene_path.stat().st_mtime_ns if gene_path else 0
    return _parse_hpo_files(
        str(hpoa_path) if hpoa_path else "",
        str(obo_path) if obo_path else "",
        str(gene_path) if gene_path else "",
        hpoa_mtime,
        obo_mtime,
        gene_mtime,
    )


@lru_cache(maxsize=4)
def _parse_hpo_files(hpoa_path, obo_path, gene_path, hpoa_mtime, obo_mtime, gene_mtime):
    terms = {}
    annotations = defaultdict(set)
    gene_annotations = defaultdict(set)
    if obo_path and Path(obo_path).is_file():
        term_id = None
        with open(obo_path, encoding="utf-8") as source:
            for line in source:
                line = line.rstrip()
                if line.startswith("[") and line.endswith("]"):
                    term_id = None
                    if line == "[Term]":
                        continue
                elif line.startswith("id: HP:") and HP_ID.fullmatch(line[4:]):
                    term_id = line[4:]
                elif term_id and line.startswith("name: "):
                    terms[term_id] = line[6:]
                elif term_id and line.startswith("synonym: ") and term_id in terms:
                    match = re.match(r'synonym: "([^"]+)"', line)
                    if match:
                        terms[term_id] += f" ({match.group(1)})"
        if "HP:0000001" not in terms:
            raise ValueError("The hp.obo file does not appear to contain HPO terms.")
    if hpoa_path and Path(hpoa_path).is_file():
        with open(hpoa_path, encoding="utf-8") as source:
            for line in source:
                if not line.strip() or line.startswith("#"):
                    continue
                columns = line.rstrip("\r\n").split("\t")
                if len(columns) < 4 or columns[2].strip().upper() == "NOT":
                    continue
                disease_id, disease_name, _, phenotype_id = columns[:4]
                if HP_ID.fullmatch(phenotype_id):
                    annotations[(disease_id, disease_name)].add(phenotype_id)
    if gene_path and Path(gene_path).is_file():
        with open(gene_path, encoding="utf-8") as source:
            for line in source:
                if not line.strip() or line.startswith("#"):
                    continue
                columns = line.rstrip("\r\n").split("\t")
                if len(columns) < 3 or not HP_ID.fullmatch(columns[2]):
                    continue
                gene_symbol = columns[1].strip()
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,29}", gene_symbol):
                    gene_annotations[gene_symbol].add(columns[2])
    return terms, annotations, gene_annotations


def score_diseases(patient_hpo_ids, annotations):
    """Return weighted phenotype overlap, explicitly not a clinical probability."""
    patient_terms = set(patient_hpo_ids)
    if not patient_terms:
        return []
    disease_frequency = defaultdict(int)
    for phenotypes in annotations.values():
        for phenotype in phenotypes:
            disease_frequency[phenotype] += 1
    total_diseases = max(1, len(annotations))

    def weight(term):
        return 1 + (total_diseases / (1 + disease_frequency[term]))

    ranked = []
    for (disease_id, disease_name), phenotype_ids in annotations.items():
        if not phenotype_ids:
            continue
        matches = patient_terms & phenotype_ids
        union = patient_terms | phenotype_ids
        similarity = 100 * sum(weight(term) for term in matches) / sum(weight(term) for term in union)
        if matches:
            ranked.append({
                "id": disease_id,
                "name": disease_name,
                "score": round(similarity),
                "matched": sorted(matches),
                "phenotypes": sorted(phenotype_ids),
            })
    return sorted(ranked, key=lambda item: (-item["score"], item["name"].casefold()))


def score_candidate_genes(patient_hpo_ids, gene_annotations):
    """Rank genes with HPO associations matching symptoms; these are not patient variants."""
    patient_terms = set(patient_hpo_ids)
    if not patient_terms:
        return []
    ranked = []
    for gene_symbol, phenotype_ids in gene_annotations.items():
        matches = patient_terms & phenotype_ids
        if matches:
            ranked.append({
                "symbol": gene_symbol,
                "score": round(100 * len(matches) / len(patient_terms)),
                "matched": sorted(matches),
            })
    return sorted(ranked, key=lambda item: (-item["score"], item["symbol"].casefold()))[:5]


# In-memory cache to avoid redundant requests for the same symptoms.
_HPO_EXTRACTION_CACHE = {}

def extract_hpo(symptoms, terms=None):
    """Use OpenAI-compatible API (OpenAI, OpenRouter, Groq, Ollama) to map symptom text to existing HPO IDs with optimized token usage."""
    if "OPENAI_API_KEY" not in os.environ:
        load_dotenv_file()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return [], "AI is not configured. In PowerShell, run .\\run_meddeck.ps1 or set your key in the .env file; do not paste it into chat or source code."
    if terms is None:
        terms = {}
    allowed_ids = set(terms) if terms else None

    # Normalize and truncate text to reduce input tokens.
    clean_symptoms = " ".join(str(symptoms or "").split())[:2500]
    if not clean_symptoms:
        return [], None

    # Check the local cache (no tokens spent on repeated queries).
    cache_key = clean_symptoms.lower()
    if cache_key in _HPO_EXTRACTION_CACHE:
        cached_results = _HPO_EXTRACTION_CACHE[cache_key]
        if allowed_ids is not None:
            return [p for p in cached_results if p["id"] in allowed_ids], None
        return cached_results, None

    # Auto-detect OpenRouter or custom endpoint
    base_url = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
    if not base_url:
        if api_key.startswith("sk-or-v1-"):
            base_url = "https://openrouter.ai/api/v1"
        else:
            base_url = "https://api.openai.com/v1"

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    payload = {
        "model": model,
        "temperature": 0.0,
        "max_tokens": 350,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "Extract explicit HPO phenotypes from the text. "
                    "Respond with JSON ONLY using this structure: "
                    '{"phenotypes":[{"hpo_id":"HP:0001250","label":"name","evidence":"quote"}]}'
                ),
            },
            {"role": "user", "content": clean_symptoms},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://127.0.0.1:5000",
        "X-Title": "MEDDECK",
    }
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw_body = response.read()
            result = json.loads(raw_body.decode("utf-8") if isinstance(raw_body, bytes) else raw_body)
        raw_content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not isinstance(raw_content, str):
            raw_content = str(raw_content or "")

        # Remove reasoning blocks (such as <think>...</think>)
        raw_content = re.sub(r"<think>.*?</think>", "", raw_content, flags=re.DOTALL)

        # Handle markdown blocks
        if "```json" in raw_content:
            raw_content = raw_content.split("```json", 1)[1].split("```", 1)[0]
        elif "```" in raw_content:
            raw_content = raw_content.split("```", 1)[1].split("```", 1)[0]

        parsed_data = None
        try:
            parsed_data = json.loads(raw_content.strip())
        except Exception:
            # Fallback: search for JSON object or array in text
            match_obj = re.search(r"(\{.*\}|\[.*\])", raw_content, re.DOTALL)
            if match_obj:
                try:
                    parsed_data = json.loads(match_obj.group(0))
                except Exception:
                    pass

        extracted = []
        if isinstance(parsed_data, dict):
            extracted = parsed_data.get("phenotypes", parsed_data.get("phenotype", []))
            if not isinstance(extracted, list):
                extracted = [extracted]
        elif isinstance(parsed_data, list):
            extracted = parsed_data

        found = []
        for item in extracted:
            if not isinstance(item, dict):
                continue
            hpo_id = str(item.get("hpo_id") or item.get("id") or "").strip()
            if HP_ID.fullmatch(hpo_id):
                label = terms.get(hpo_id) if (terms and hpo_id in terms) else str(item.get("label") or item.get("name") or hpo_id)
                evidence = str(item.get("evidence") or item.get("symptom") or "")[:300]
                if allowed_ids is None or hpo_id in allowed_ids or not terms:
                    found.append({"id": hpo_id, "label": label, "evidence": evidence})

        # Regex fallback: extract any HP:\d{7} IDs present in text
        if not found:
            for hp_code in set(re.findall(r"HP:\d{7}", raw_content)):
                label = terms.get(hp_code, hp_code) if terms else hp_code
                if allowed_ids is None or hp_code in allowed_ids or not terms:
                    found.append({"id": hp_code, "label": label, "evidence": "Extracted from symptom analysis"})

        unique = {item["id"]: item for item in found}
        result_list = list(unique.values())
        if result_list:
            _HPO_EXTRACTION_CACHE[cache_key] = result_list
        return result_list, None
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return [], "Authentication error (401): Your key is invalid or expired. Check your .env file."
        elif exc.code == 429:
            return [], "Rate limit or quota exceeded (429): Your account may not have enough credits or may have exceeded its request limit."
        return [], f"API error ({exc.code} {exc.reason}). Check your configuration."
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, TypeError) as exc:
        return [], f"Phenotype extraction failed ({type(exc).__name__}). Please try again."


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("MEDDECK_SECRET_KEY"),
        SECRET_KEY_FILE=os.environ.get("MEDDECK_SECRET_FILE", str(DEFAULT_SECRET_FILE)),
        DATABASE=os.environ.get("MEDDECK_DATABASE", str(DEFAULT_DB)),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("MEDDECK_HTTPS", "0") == "1",
    )
    if test_config:
        app.config.update(test_config)
    if not app.config["SECRET_KEY"]:
        app.config["SECRET_KEY"] = load_or_create_secret(app.config["SECRET_KEY_FILE"])
    init_db(app.config["DATABASE"])

    @app.before_request
    def protect_posts():
        if request.method == "POST":
            token = request.form.get("csrf_token", "")
            expected = session.get("csrf_token", "")
            if not expected or not secrets.compare_digest(token, expected):
                abort(400, "Form expired. Reload the page and try again.")

    @app.context_processor
    def common_context():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return {"csrf_token": session["csrf_token"], "current_user": g.get("user"), "roles": ROLES}

    @app.before_request
    def load_user():
        user_id = session.get("user_id")
        g.user = None
        if user_id:
            with database(app.config["DATABASE"]) as db:
                g.user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if g.user is None:
                session.clear()

    def require_login():
        if g.user is None:
            flash("Please sign in to continue.", "info")
            return redirect(url_for("login"))
        return None

    def community_for(db, name, basis):
        name = name.strip()[:100]
        if not name:
            return None
        db.execute("INSERT OR IGNORE INTO communities(name, basis) VALUES (?, ?)", (name, basis))
        return db.execute(
            "SELECT id FROM communities WHERE name = ? COLLATE NOCASE AND basis = ?", (name, basis)
        ).fetchone()["id"]

    def join_community(db, user_id, name, basis):
        community_id = community_for(db, name, basis)
        if community_id:
            db.execute("INSERT OR IGNORE INTO memberships(user_id, community_id) VALUES (?, ?)", (user_id, community_id))

    @app.get("/")
    def home():
        return render_template("home.html")

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            name = request.form.get("name", "").strip()[:100]
            email = request.form.get("email", "").strip().lower()[:254]
            password = request.form.get("password", "")
            role = request.form.get("role", "")
            age_raw = request.form.get("age", "")
            sex = request.form.get("sex", "")
            diagnosis = request.form.get("diagnosed") == "yes"
            condition = request.form.get("condition", "").strip()[:160]
            symptoms = request.form.get("symptoms", "").strip()[:6000]
            consent_ai = request.form.get("consent_ai") == "yes"
            consent_gene_graph = request.form.get("consent_gene_graph") == "yes"
            try:
                age = int(age_raw)
            except ValueError:
                age = 0
            if not name or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                flash("Please enter a valid name and email address.", "error")
            elif len(password) < 10:
                flash("Password must be at least 10 characters.", "error")
            elif not 18 <= age <= 120:
                flash("This version only allows adults (18 or older) to create an account.", "error")
            elif sex not in SEX_OPTIONS or role not in ROLES:
                flash("Please select valid sex and role options.", "error")
            elif role == "patient" and not symptoms:
                flash("Please tell us what symptoms you wish to share.", "error")
            elif role == "patient" and diagnosis and not condition:
                flash("Please enter your diagnosed condition.", "error")
            elif role != "patient" and not condition:
                flash("Please indicate the disease of interest.", "error")
            else:
                try:
                    with database(app.config["DATABASE"]) as db:
                        cursor = db.execute(
                            """INSERT INTO users(name,email,password_hash,age,sex,role,diagnosed,condition,symptoms,
                               consent_ai,consent_gene_graph) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                            (name, email, generate_password_hash(password), age, sex, role,
                             int(diagnosis if role == "patient" else True), condition, symptoms,
                             int(consent_ai), int(consent_gene_graph)),
                        )
                        user_id = cursor.lastrowid
                        if role != "patient" or diagnosis:
                            join_community(db, user_id, condition, "disease")
                    session.clear()
                    session["user_id"] = user_id
                    flash("Your profile is ready. You can review or change your details anytime.", "success")
                    return redirect(url_for("dashboard"))
                except sqlite3.IntegrityError:
                    flash("An account with that email already exists.", "error")
        return render_template("register.html", sex_options=SEX_OPTIONS)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            with database(app.config["DATABASE"]) as db:
                user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if user and check_password_hash(user["password_hash"], password):
                session.clear()
                session["user_id"] = user["id"]
                return redirect(url_for("dashboard"))
            flash("Incorrect email or password.", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("home"))

    @app.route("/profile", methods=["GET", "POST"])
    def profile():
        denied = require_login()
        if denied:
            return denied
        if request.method == "POST":
            name = request.form.get("name", "").strip()[:100]
            symptoms = request.form.get("symptoms", "").strip()[:6000]
            condition = request.form.get("condition", "").strip()[:160]
            consent_ai = request.form.get("consent_ai") == "yes"
            consent_gene_graph = request.form.get("consent_gene_graph") == "yes"
            if not name or (g.user["role"] == "patient" and not symptoms):
                flash("Please check the name and required clinical information.", "error")
            else:
                with database(app.config["DATABASE"]) as db:
                    db.execute(
                        """UPDATE users SET name=?, symptoms=?, condition=?, consent_ai=?,
                           consent_gene_graph=? WHERE id=?""",
                        (name, symptoms, condition, int(consent_ai), int(consent_gene_graph), g.user["id"]),
                    )
                    if condition and (g.user["role"] != "patient" or g.user["diagnosed"]):
                        join_community(db, g.user["id"], condition, "disease")
                flash("Profile updated.", "success")
                return redirect(url_for("dashboard"))
        return render_template("profile.html", user=g.user)

    @app.post("/delete-account")
    def delete_account():
        denied = require_login()
        if denied:
            return denied
        if request.form.get("confirmation") != "DELETE":
            flash("Type DELETE to confirm deletion.", "error")
            return redirect(url_for("profile"))
        with database(app.config["DATABASE"]) as db:
            db.execute("DELETE FROM users WHERE id=?", (g.user["id"],))
        session.clear()
        flash("Account and associated data have been deleted from this instance.", "success")
        return redirect(url_for("home"))

    @app.get("/dashboard")
    def dashboard():
        denied = require_login()
        if denied:
            return denied
        terms, annotations, gene_annotations = load_hpo_data()
        hpo_items = json.loads(g.user["hpo_json"])
        candidates = score_diseases([item["id"] for item in hpo_items], annotations)[:3]
        gene_candidates = json.loads(g.user["gene_candidates_json"])
        for candidate in gene_candidates:
            candidate["matched_labels"] = [
                terms.get(phenotype_id, phenotype_id) for phenotype_id in candidate["matched"]
            ]
        disease_phenotypes = {item["id"]: [terms.get(p, p) for p in item["matched"]] for item in candidates}
        with database(app.config["DATABASE"]) as db:
            communities = db.execute(
                """SELECT c.*, (SELECT COUNT(*) FROM memberships m WHERE m.community_id=c.id) AS members
                   FROM communities c JOIN memberships m ON m.community_id=c.id
                   WHERE m.user_id=? ORDER BY c.basis, c.name""", (g.user["id"],)
            ).fetchall()
            post_rows = db.execute(
                """SELECT p.body,p.created_at,u.name,c.name AS community_name,c.id AS community_id
                   FROM posts p JOIN users u ON u.id=p.user_id JOIN communities c ON c.id=p.community_id
                   JOIN memberships m ON m.community_id=c.id AND m.user_id=?
                   ORDER BY p.created_at DESC LIMIT 20""", (g.user["id"],)
            ).fetchall()

        # Build personal patient network graph
        nodes_map = {}
        links_list = []
        user_node_id = f"u_{g.user['id']}"
        nodes_map[user_node_id] = {
            "id": user_node_id,
            "name": g.user["name"],
            "type": "user",
            "radius": 24,
        }

        for item in hpo_items:
            hpo_node_id = f"h_{item['id']}"
            nodes_map[hpo_node_id] = {
                "id": hpo_node_id,
                "name": item.get("label") or item["id"],
                "type": "phenotype",
                "hpo_id": item["id"],
                "radius": 20,
            }
            links_list.append({
                "source": user_node_id,
                "target": hpo_node_id,
                "type": "symptom",
                "label": "presents",
            })

        for gc in gene_candidates:
            gene_node_id = f"g_{gc['symbol']}"
            nodes_map[gene_node_id] = {
                "id": gene_node_id,
                "name": gc["symbol"],
                "type": "gene",
                "radius": 26,
            }
            links_list.append({
                "source": user_node_id,
                "target": gene_node_id,
                "type": "candidate",
                "label": f"{gc.get('score', '')}%",
            })
            for mid in gc.get("matched", []):
                hpo_mid = f"h_{mid}"
                if hpo_mid in nodes_map:
                    links_list.append({
                        "source": gene_node_id,
                        "target": hpo_mid,
                        "type": "expresses",
                        "label": "associated",
                    })

        for comm in communities:
            comm_node_id = f"c_{comm['id']}"
            nodes_map[comm_node_id] = {
                "id": comm_node_id,
                "name": comm["name"],
                "type": "community",
                "radius": 24,
            }
            gene_match = f"g_{comm['name']}"
            if gene_match in nodes_map:
                links_list.append({
                    "source": comm_node_id,
                    "target": gene_match,
                    "type": "community_link",
                })
            else:
                links_list.append({
                    "source": user_node_id,
                    "target": comm_node_id,
                    "type": "member_of",
                })

        patient_graph_json = json.dumps({"nodes": list(nodes_map.values()), "links": links_list}, ensure_ascii=False)

        return render_template(
            "dashboard.html", user=g.user, hpo_items=hpo_items, candidates=candidates,
            gene_candidates=gene_candidates, gene_data_loaded=bool(gene_annotations),
            disease_phenotypes=disease_phenotypes, communities=communities, posts=post_rows,
            hpo_loaded=bool(terms and annotations),
            ai_configured=bool(os.environ.get("OPENAI_API_KEY")),
            patient_graph_json=patient_graph_json,
            pubmed_url="https://pubmed.ncbi.nlm.nih.gov/?term=" + quote_plus(g.user["condition"] or ""),
        )

    @app.post("/analyze")
    def analyze():
        denied = require_login()
        if denied:
            return denied
        if g.user["role"] != "patient":
            abort(403)
        terms, annotations, gene_annotations = load_hpo_data()
        if not g.user["consent_ai"]:
            flash("You have not consented to send symptoms to the AI provider. Update your profile/consent before analyzing.", "error")
            return redirect(url_for("dashboard"))
        phenotypes, error = extract_hpo(g.user["symptoms"], terms)
        if error:
            flash(error, "error")
        elif not phenotypes:
            flash("No validated HPO terms were found in the text; you can edit your symptoms and try again.", "info")
        else:
            gene_candidates = score_candidate_genes(
                [item["id"] for item in phenotypes], gene_annotations
            )
            with database(app.config["DATABASE"]) as db:
                db.execute(
                    "UPDATE users SET hpo_json=?, gene_candidates_json=? WHERE id=?",
                    (json.dumps(phenotypes, ensure_ascii=False),
                     json.dumps(gene_candidates, ensure_ascii=False), g.user["id"]),
                )
                for candidate in gene_candidates:
                    join_community(db, g.user["id"], candidate["symbol"], "candidate_gene")
            if gene_annotations:
                flash(
                    f"Found {len(phenotypes)} HPO terms and {len(gene_candidates)} associated candidate genes. This does not mean you have variants or affected genes.",
                    "success",
                )
            else:
                flash(f"Identified {len(phenotypes)} HPO phenotypes. (Note: genes_to_phenotype.txt in Downloads is required to cross-reference associated genes).", "info")
        return redirect(url_for("dashboard"))

    @app.post("/join")
    def join():
        denied = require_login()
        if denied:
            return denied
        disease_id = request.form.get("disease_id", "")
        _, annotations, _ = load_hpo_data()
        disease = next((key for key in annotations if key[0] == disease_id), None)
        if not disease:
            flash("Disease not found in the loaded HPO data.", "error")
        else:
            with database(app.config["DATABASE"]) as db:
                join_community(db, g.user["id"], disease[1], "disease")
            flash(f"You joined the {disease[1]} community.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/mark-diagnosed")
    def mark_diagnosed():
        denied = require_login()
        if denied:
            return denied
        if g.user["role"] != "patient" or g.user["diagnosed"]:
            abort(403)
        genes = request.form.get("genes", "").strip()[:1000]
        symptoms = request.form.get("symptoms", "").strip()[:6000]
        if not genes or not symptoms:
            flash("Please complete the affected genes and your current symptoms.", "error")
        else:
            gene_names = [item.strip() for item in re.split(r"[,;\n]", genes) if item.strip()]
            with database(app.config["DATABASE"]) as db:
                db.execute("UPDATE users SET diagnosed=1, genes=?, symptoms=? WHERE id=?",
                           (genes, symptoms, g.user["id"]))
                for gene in gene_names:
                    join_community(db, g.user["id"], gene, "gene")
                if g.user["condition"]:
                    join_community(db, g.user["id"], g.user["condition"], "disease")
            flash("Profile updated; communities by gene were created or updated.", "success")
        return redirect(url_for("dashboard"))

    @app.route("/community/<int:community_id>", methods=["GET", "POST"])
    def community(community_id):
        denied = require_login()
        if denied:
            return denied
        terms, _, _ = load_hpo_data()
        with database(app.config["DATABASE"]) as db:
            member = db.execute("SELECT 1 FROM memberships WHERE user_id=? AND community_id=?",
                                (g.user["id"], community_id)).fetchone()
            if not member:
                abort(403)
            if request.method == "POST":
                body = request.form.get("body", "").strip()[:2000]
                if not body:
                    flash("Message cannot be empty.", "error")
                else:
                    db.execute("INSERT INTO posts(community_id,user_id,body) VALUES (?,?,?)",
                               (community_id, g.user["id"], body))
                    return redirect(url_for("community", community_id=community_id))
            community_info = db.execute("SELECT * FROM communities WHERE id=?", (community_id,)).fetchone()
            posts = db.execute(
                """SELECT p.body,p.created_at,u.name FROM posts p JOIN users u ON u.id=p.user_id
                   WHERE p.community_id=? ORDER BY p.created_at DESC LIMIT 100""", (community_id,)
            ).fetchall()
            members = db.execute(
                """SELECT u.id, u.name, u.role, u.consent_gene_graph, u.hpo_json, u.gene_candidates_json
                   FROM users u JOIN memberships m ON m.user_id=u.id
                   WHERE m.community_id=?""", (community_id,)
            ).fetchall()

        # Build community subgraph
        nodes_map = {}
        links_list = []
        comm_node_id = f"c_{community_id}"
        nodes_map[comm_node_id] = {
            "id": comm_node_id,
            "name": community_info["name"],
            "type": "community",
            "radius": 30,
        }

        # If it's a gene community, add gene node
        if community_info["basis"] in ("gene", "candidate_gene"):
            gene_node_id = f"g_{community_info['name']}"
            nodes_map[gene_node_id] = {
                "id": gene_node_id,
                "name": community_info["name"],
                "type": "gene",
                "radius": 26,
            }
            links_list.append({"source": comm_node_id, "target": gene_node_id, "type": "basis"})

        for m_row in members:
            # Show real name if consent given or if viewing self
            is_self = m_row["id"] == g.user["id"]
            display_name = m_row["name"] if (m_row["consent_gene_graph"] or is_self) else f"Participant #{m_row['id']}"
            m_node_id = f"u_{m_row['id']}"
            nodes_map[m_node_id] = {
                "id": m_node_id,
                "name": display_name,
                "type": "user",
                "radius": 22,
            }
            links_list.append({"source": m_node_id, "target": comm_node_id, "type": "member", "label": "member"})

            # If user has consent, connect their HPO phenotypes
            if m_row["consent_gene_graph"] or is_self:
                try:
                    for hpo_item in json.loads(m_row["hpo_json"]):
                        hpo_id = hpo_item.get("id")
                        if hpo_id:
                            hpo_node_id = f"h_{hpo_id}"
                            if hpo_node_id not in nodes_map:
                                nodes_map[hpo_node_id] = {
                                    "id": hpo_node_id,
                                    "name": terms.get(hpo_id, hpo_item.get("label", hpo_id)),
                                    "type": "phenotype",
                                    "hpo_id": hpo_id,
                                    "radius": 18,
                                }
                            links_list.append({"source": m_node_id, "target": hpo_node_id, "type": "phenotype_link"})
                except Exception:
                    pass

        community_graph_json = json.dumps({"nodes": list(nodes_map.values()), "links": links_list}, ensure_ascii=False)

        return render_template(
            "community.html",
            community=community_info,
            posts=posts,
            members=members,
            community_graph_json=community_graph_json,
        )

    @app.post("/community/<int:community_id>/leave")
    def leave_community(community_id):
        denied = require_login()
        if denied:
            return denied
        with database(app.config["DATABASE"]) as db:
            db.execute("DELETE FROM memberships WHERE user_id=? AND community_id=?",
                       (g.user["id"], community_id))
        return redirect(url_for("dashboard"))

    @app.get("/atlas")
    def atlas():
        terms, annotations, _ = load_hpo_data()
        edges = []
        nodes_map = {}
        links_list = []
        for (disease_id, disease_name), phenotypes in list(annotations.items())[:150]:
            d_node_id = f"d_{disease_id}"
            nodes_map[d_node_id] = {
                "id": d_node_id,
                "name": disease_name,
                "type": "disease",
                "radius": 24,
            }
            for phenotype_id in sorted(phenotypes)[:10]:
                p_label = terms.get(phenotype_id, phenotype_id)
                edges.append({"disease": disease_name, "disease_id": disease_id,
                              "phenotype": p_label, "phenotype_id": phenotype_id})
                p_node_id = f"h_{phenotype_id}"
                if p_node_id not in nodes_map:
                    nodes_map[p_node_id] = {
                        "id": p_node_id,
                        "name": p_label,
                        "type": "phenotype",
                        "hpo_id": phenotype_id,
                        "radius": 18,
                    }
                links_list.append({"source": d_node_id, "target": p_node_id, "type": "disease_phenotype"})

        atlas_graph_json = json.dumps({"nodes": list(nodes_map.values()), "links": links_list}, ensure_ascii=False)
        return render_template(
            "atlas.html",
            edges=edges,
            sources=SOURCES,
            data_loaded=bool(annotations),
            atlas_graph_json=atlas_graph_json,
        )

    @app.get("/gene-graph")
    def gene_graph():
        denied = require_login()
        if denied:
            return denied
        terms, _, _ = load_hpo_data()
        with database(app.config["DATABASE"]) as db:
            rows = db.execute(
                """SELECT id, name, gene_candidates_json, hpo_json FROM users
                   WHERE consent_gene_graph=1 AND role='patient' ORDER BY name COLLATE NOCASE"""
            ).fetchall()
            communities = db.execute("SELECT * FROM communities").fetchall()

        grouped = defaultdict(list)
        nodes_map = {}
        links_list = []

        # Add community nodes
        for comm in communities:
            comm_node_id = f"c_{comm['id']}"
            nodes_map[comm_node_id] = {
                "id": comm_node_id,
                "name": comm["name"],
                "type": "community",
                "basis": comm["basis"],
                "radius": 24,
            }

        total_participants = len(rows)
        phenotypes_set = set()

        for row in rows:
            u_node_id = f"u_{row['id']}"
            nodes_map[u_node_id] = {
                "id": u_node_id,
                "name": row["name"],
                "type": "user",
                "radius": 22,
            }

            # Link patient HPO phenotypes
            try:
                hpos = json.loads(row["hpo_json"])
                for hp in hpos:
                    hp_id = hp.get("id")
                    if hp_id:
                        phenotypes_set.add(hp_id)
                        hp_node_id = f"h_{hp_id}"
                        if hp_node_id not in nodes_map:
                            nodes_map[hp_node_id] = {
                                "id": hp_node_id,
                                "name": terms.get(hp_id, hp.get("label", hp_id)),
                                "type": "phenotype",
                                "hpo_id": hp_id,
                                "radius": 18,
                            }
                        links_list.append({
                            "source": u_node_id,
                            "target": hp_node_id,
                            "type": "phenotype",
                        })
            except Exception:
                pass

            # Link patient candidate genes
            for candidate in json.loads(row["gene_candidates_json"]):
                symbol = candidate.get("symbol", "")
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,29}", symbol):
                    grouped[symbol].append({"name": row["name"]})
                    g_node_id = f"g_{symbol}"
                    if g_node_id not in nodes_map:
                        nodes_map[g_node_id] = {
                            "id": g_node_id,
                            "name": symbol,
                            "type": "gene",
                            "radius": 26,
                        }
                    links_list.append({
                        "source": u_node_id,
                        "target": g_node_id,
                        "type": "candidate_gene",
                        "label": f"{candidate.get('score', '')}%",
                    })
                    # Link gene to matched HPO phenotypes
                    for mid in candidate.get("matched", []):
                        hp_mid = f"h_{mid}"
                        if hp_mid in nodes_map:
                            links_list.append({
                                "source": g_node_id,
                                "target": hp_mid,
                                "type": "gene_phenotype",
                            })

        # Connect communities to corresponding genes
        for comm in communities:
            comm_node_id = f"c_{comm['id']}"
            g_target = f"g_{comm['name']}"
            if g_target in nodes_map:
                links_list.append({
                    "source": comm_node_id,
                    "target": g_target,
                    "type": "community_gene",
                })

        genes = [
            {"symbol": symbol, "members": members}
            for symbol, members in sorted(grouped.items(), key=lambda item: item[0].casefold())
        ]

        graph_json = json.dumps({"nodes": list(nodes_map.values()), "links": links_list}, ensure_ascii=False)

        stats = {
            "genes_count": len(genes),
            "participants_count": total_participants,
            "phenotypes_count": len(phenotypes_set),
            "communities_count": len(communities),
        }

        return render_template("gene_graph.html", genes=genes, graph_json=graph_json, stats=stats)

    @app.post("/join-candidate-gene")
    def join_candidate_gene():
        denied = require_login()
        if denied:
            return denied
        symbol = request.form.get("gene", "").strip()
        _, _, gene_annotations = load_hpo_data()
        if gene_annotations and symbol not in gene_annotations:
            flash("That gene was not found in the loaded HPO associations.", "error")
        else:
            with database(app.config["DATABASE"]) as db:
                join_community(db, g.user["id"], symbol, "candidate_gene")
            flash(f"You joined the discussion community for people interested in {symbol}.", "success")
        return redirect(url_for("dashboard"))

    @app.get("/sources")
    def sources():
        return render_template("sources.html", sources=SOURCES)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host=os.environ.get("MEDDECK_HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "5000")),
            debug=os.environ.get("MEDDECK_DEBUG", "0") == "1")
