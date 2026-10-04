import json
import os
import re
import secrets
import sqlite3
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
DEFAULT_DB = BASE_DIR / "instance" / "meddeck.sqlite"
DEFAULT_SECRET_FILE = BASE_DIR / "instance" / "flask-secret.key"
HP_ID = re.compile(r"^HP:\d{7}$")
ROLES = {"patient": "Paciente", "doctor": "Médico", "researcher": "Investigador"}
SEX_OPTIONS = {"female": "Mujer", "male": "Hombre", "intersex": "Intersexual", "prefer_not": "Prefiero no decirlo"}

SOURCES = [
    ("HPO", "Fenotipos y relaciones enfermedad-fenotipo", "https://hpo.jax.org/"),
    ("Orphanet", "Información y catálogo de enfermedades raras", "https://www.orpha.net/"),
    ("OMIM", "Genes y fenotipos hereditarios; acceso sujeto a sus condiciones", "https://www.omim.org/"),
    ("PubMed", "Literatura biomédica; las consultas abren resultados de NCBI", "https://pubmed.ncbi.nlm.nih.gov/"),
    ("MONDO", "Identificadores de enfermedades", "https://mondo.monarchinitiative.org/"),
    ("ClinVar", "Variantes genéticas y evidencias clínicas", "https://www.ncbi.nlm.nih.gov/clinvar/"),
    ("ClinicalTrials.gov", "Ensayos clínicos registrados", "https://clinicaltrials.gov/"),
    ("NORD", "Recursos y organizaciones de enfermedades raras", "https://rarediseases.org/"),
    ("Global Genes", "Recursos comunitarios", "https://globalgenes.org/"),
    ("EURORDIS", "Organizaciones y recursos europeos", "https://www.eurordis.org/"),
    ("Rare Disease UK", "Apoyo e información", "https://www.rarediseaseuk.org/"),
    ("Genetic Alliance", "Recursos de genética", "https://geneticalliance.org.uk/"),
    ("NIH RePORTER", "Proyectos de investigación financiados por NIH", "https://reporter.nih.gov/"),
]


def load_or_create_secret(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        secret = secrets.token_urlsafe(48)
        try:
            with path.open("x", encoding="utf-8") as secret_file:
                secret_file.write(secret)
        except FileExistsError:
            return path.read_text(encoding="utf-8").strip()
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
            raise ValueError("El archivo hp.obo no parece contener términos HPO.")
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


def extract_hpo(symptoms, terms):
    """Use the official OpenAI API to map symptom text to existing HPO IDs."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return [], "La IA no está configurada. En PowerShell, ejecuta .\\run_meddeck.ps1 e introduce una clave nueva en el prompt oculto; no la pegues en el chat ni en el código."
    if not terms:
        return [], "Falta cargar la ontología HPO (hp.obo); no se pueden validar términos."
    allowed_ids = set(terms)
    payload = {
        "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "Extrae únicamente fenotipos explícitamente descritos en el texto. "
                    "No diagnostiques ni infieras. Responde JSON con la forma "
                    '{"phenotypes":[{"hpo_id":"HP:0000000","evidence":"frase textual"}]}.'
                ),
            },
            {"role": "user", "content": symptoms[:6000]},
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read())
        content = result["choices"][0]["message"]["content"]
        extracted = json.loads(content).get("phenotypes", [])
        found = []
        for item in extracted:
            hpo_id = item.get("hpo_id", "")
            if HP_ID.fullmatch(hpo_id) and hpo_id in allowed_ids:
                found.append({"id": hpo_id, "label": terms[hpo_id], "evidence": str(item.get("evidence", ""))[:300]})
        unique = {item["id"]: item for item in found}
        return list(unique.values()), None
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, TypeError) as exc:
        return [], f"No se pudo completar la extracción de fenotipos ({type(exc).__name__}). Inténtalo de nuevo."


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
                abort(400, "Formulario vencido. Recarga la página e inténtalo de nuevo.")

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
            flash("Inicia sesión para continuar.", "info")
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
                flash("Escribe un nombre y un correo electrónico válido.", "error")
            elif len(password) < 10:
                flash("La contraseña debe tener al menos 10 caracteres.", "error")
            elif not 18 <= age <= 120:
                flash("Esta versión solo permite crear cuentas a personas adultas (18 años o más).", "error")
            elif sex not in SEX_OPTIONS or role not in ROLES:
                flash("Selecciona opciones válidas de sexo y rol.", "error")
            elif role == "patient" and not symptoms:
                flash("Cuéntanos qué síntomas deseas compartir.", "error")
            elif role == "patient" and diagnosis and not condition:
                flash("Escribe la condición ya diagnosticada.", "error")
            elif role != "patient" and not condition:
                flash("Indica la enfermedad de interés.", "error")
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
                    flash("Tu perfil está listo. Puedes revisar o cambiar tus datos cuando quieras.", "success")
                    return redirect(url_for("dashboard"))
                except sqlite3.IntegrityError:
                    flash("Ya existe una cuenta con ese correo.", "error")
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
            flash("Correo o contraseña incorrectos.", "error")
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
                flash("Revisa el nombre y la información clínica requerida.", "error")
            else:
                with database(app.config["DATABASE"]) as db:
                    db.execute(
                        """UPDATE users SET name=?, symptoms=?, condition=?, consent_ai=?,
                           consent_gene_graph=? WHERE id=?""",
                        (name, symptoms, condition, int(consent_ai), int(consent_gene_graph), g.user["id"]),
                    )
                    if condition and (g.user["role"] != "patient" or g.user["diagnosed"]):
                        join_community(db, g.user["id"], condition, "disease")
                flash("Perfil actualizado.", "success")
                return redirect(url_for("dashboard"))
        return render_template("profile.html", user=g.user)

    @app.post("/delete-account")
    def delete_account():
        denied = require_login()
        if denied:
            return denied
        if request.form.get("confirmation") != "ELIMINAR":
            flash("Escribe ELIMINAR para confirmar el borrado.", "error")
            return redirect(url_for("profile"))
        with database(app.config["DATABASE"]) as db:
            db.execute("DELETE FROM users WHERE id=?", (g.user["id"],))
        session.clear()
        flash("La cuenta y los datos asociados fueron eliminados de esta instancia.", "success")
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
        return render_template(
            "dashboard.html", user=g.user, hpo_items=hpo_items, candidates=candidates,
            gene_candidates=gene_candidates, gene_data_loaded=bool(gene_annotations),
            disease_phenotypes=disease_phenotypes, communities=communities, posts=post_rows,
            hpo_loaded=bool(terms and annotations),
            ai_configured=bool(os.environ.get("OPENAI_API_KEY")),
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
        if not annotations or not terms:
            flash("No se pudieron cargar los archivos HPO. Comprueba que phenotype.hpoa y hp.obo estén en Descargas o configura HPOA_PATH y HPO_OBO_PATH.", "error")
            return redirect(url_for("dashboard"))
        if not g.user["consent_ai"]:
            flash("No diste consentimiento para enviar síntomas al proveedor de IA. Actualiza tu perfil/consentimiento antes de analizar.", "error")
            return redirect(url_for("dashboard"))
        phenotypes, error = extract_hpo(g.user["symptoms"], terms)
        if error:
            flash(error, "error")
        elif not phenotypes:
            flash("No se encontraron términos HPO validados en el texto; puedes editar los síntomas e intentarlo de nuevo.", "info")
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
                    f"Se encontraron {len(phenotypes)} términos HPO y {len(gene_candidates)} genes candidatos asociados. No significa que tengas variantes ni genes afectados.",
                    "success",
                )
            else:
                flash("Se encontraron términos HPO, pero falta genes_to_phenotype.txt para contrastar genes candidatos.", "info")
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
            flash("No se encontró esa enfermedad en los datos HPO cargados.", "error")
        else:
            with database(app.config["DATABASE"]) as db:
                join_community(db, g.user["id"], disease[1], "disease")
            flash(f"Te uniste a la comunidad de {disease[1]}.", "success")
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
            flash("Completa los genes afectados y tus síntomas actuales.", "error")
        else:
            gene_names = [item.strip() for item in re.split(r"[,;\n]", genes) if item.strip()]
            with database(app.config["DATABASE"]) as db:
                db.execute("UPDATE users SET diagnosed=1, genes=?, symptoms=? WHERE id=?",
                           (genes, symptoms, g.user["id"]))
                for gene in gene_names:
                    join_community(db, g.user["id"], gene, "gene")
                if g.user["condition"]:
                    join_community(db, g.user["id"], g.user["condition"], "disease")
            flash("Perfil actualizado; se crearon o actualizaron comunidades por gen.", "success")
        return redirect(url_for("dashboard"))

    @app.route("/community/<int:community_id>", methods=["GET", "POST"])
    def community(community_id):
        denied = require_login()
        if denied:
            return denied
        with database(app.config["DATABASE"]) as db:
            member = db.execute("SELECT 1 FROM memberships WHERE user_id=? AND community_id=?",
                                (g.user["id"], community_id)).fetchone()
            if not member:
                abort(403)
            if request.method == "POST":
                body = request.form.get("body", "").strip()[:2000]
                if not body:
                    flash("El mensaje no puede estar vacío.", "error")
                else:
                    db.execute("INSERT INTO posts(community_id,user_id,body) VALUES (?,?,?)",
                               (community_id, g.user["id"], body))
                    return redirect(url_for("community", community_id=community_id))
            community_info = db.execute("SELECT * FROM communities WHERE id=?", (community_id,)).fetchone()
            posts = db.execute(
                """SELECT p.body,p.created_at,u.name FROM posts p JOIN users u ON u.id=p.user_id
                   WHERE p.community_id=? ORDER BY p.created_at DESC LIMIT 100""", (community_id,)
            ).fetchall()
        return render_template("community.html", community=community_info, posts=posts)

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
        for (disease_id, disease_name), phenotypes in list(annotations.items())[:150]:
            for phenotype_id in sorted(phenotypes)[:10]:
                edges.append({"disease": disease_name, "disease_id": disease_id,
                              "phenotype": terms.get(phenotype_id, phenotype_id), "phenotype_id": phenotype_id})
        return render_template("atlas.html", edges=edges, sources=SOURCES, data_loaded=bool(annotations))

    @app.get("/gene-graph")
    def gene_graph():
        denied = require_login()
        if denied:
            return denied
        with database(app.config["DATABASE"]) as db:
            rows = db.execute(
                """SELECT id, name, gene_candidates_json FROM users
                   WHERE consent_gene_graph=1 AND role='patient' ORDER BY name COLLATE NOCASE"""
            ).fetchall()
        grouped = defaultdict(list)
        for row in rows:
            for candidate in json.loads(row["gene_candidates_json"]):
                symbol = candidate.get("symbol", "")
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,29}", symbol):
                    grouped[symbol].append({"name": row["name"]})
        genes = [
            {"symbol": symbol, "members": members}
            for symbol, members in sorted(grouped.items(), key=lambda item: item[0].casefold())
        ]
        return render_template("gene_graph.html", genes=genes)

    @app.post("/join-candidate-gene")
    def join_candidate_gene():
        denied = require_login()
        if denied:
            return denied
        symbol = request.form.get("gene", "").strip()
        _, _, gene_annotations = load_hpo_data()
        if symbol not in gene_annotations:
            flash("No se encontró ese gen en las asociaciones HPO cargadas.", "error")
        else:
            with database(app.config["DATABASE"]) as db:
                join_community(db, g.user["id"], symbol, "candidate_gene")
            flash(f"Te uniste a la comunidad de discusión para personas interesadas en el gen {symbol}.", "success")
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
