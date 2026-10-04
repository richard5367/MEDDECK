import json
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from meddeck import create_app, extract_hpo, load_hpo_data, score_diseases


class HpoTests(unittest.TestCase):
    def test_weighted_overlap_is_similarity_not_zero_for_match(self):
        annotations = {
            ("OMIM:1", "Test condition one"): {"HP:0000001", "HP:0000002"},
            ("ORPHA:2", "Test condition two"): {"HP:0000003"},
        }
        results = score_diseases(["HP:0000001"], annotations)
        self.assertEqual(results[0]["id"], "OMIM:1")
        self.assertGreater(results[0]["score"], 0)
        self.assertLessEqual(results[0]["score"], 100)
        self.assertEqual(results[0]["matched"], ["HP:0000001"])

    def test_no_matches_return_no_candidates(self):
        self.assertEqual(score_diseases(["HP:0000010"], {("OMIM:1", "Test"): {"HP:0000001"}}), [])

    def test_loads_hpoa_and_obo(self):
        with tempfile.TemporaryDirectory() as directory:
            obo = Path(directory) / "hp.obo"
            hpoa = Path(directory) / "phenotype.hpoa"
            obo.write_text("[Term]\nid: HP:0000001\nname: Test phenotype\n", encoding="utf-8")
            hpoa.write_text("# header\nOMIM:1\tTest condition\t\tHP:0000001\tPMID:1\n", encoding="utf-8")
            with patch("meddeck.Path.home", return_value=Path(directory)), \
                 patch.dict(os.environ, {"HPO_GENE_PATH": ""}):
                terms, annotations, gene_annotations = load_hpo_data(hpoa, obo)
        self.assertEqual(terms["HP:0000001"], "Test phenotype")
        self.assertEqual(annotations[("OMIM:1", "Test condition")], {"HP:0000001"})
        self.assertEqual(gene_annotations, {})

    def test_loads_default_downloaded_hpo_files(self):
        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory) / "Downloads"
            downloads.mkdir()
            (downloads / "hp.obo").write_text(
                "[Term]\nid: HP:0000001\nname: Test phenotype\n", encoding="utf-8"
            )
            (downloads / "phenotype.hpoa").write_text(
                "#header\ndb:1\tTest condition\t\tHP:0000001\tPMID:1\n", encoding="utf-8"
            )
            (downloads / "genes_to_phenotype.txt").write_text(
                "ncbi_gene_id\tgene_symbol\thpo_id\thpo_name\n"
                "16\tAARS1\tHP:0000001\tTest phenotype\n", encoding="utf-8"
            )
            with patch("meddeck.Path.home", return_value=Path(directory)), \
                 patch.dict(os.environ, {"HPOA_PATH": "", "HPO_OBO_PATH": "", "HPO_GENE_PATH": ""}):
                terms, annotations, gene_annotations = load_hpo_data()
        self.assertEqual(terms["HP:0000001"], "Test phenotype")
        self.assertIn(("db:1", "Test condition"), annotations)
        self.assertEqual(gene_annotations["AARS1"], {"HP:0000001"})

    def test_ranks_candidate_genes_only_from_shared_hpo_terms(self):
        from meddeck import score_candidate_genes

        candidates = score_candidate_genes(
            ["HP:0000001", "HP:0000002"],
            {"AARS1": {"HP:0000001"}, "GENE2": {"HP:0000003"}},
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["symbol"], "AARS1")
        self.assertEqual(candidates[0]["matched"], ["HP:0000001"])
        self.assertEqual(candidates[0]["score"], 50)

    def test_extract_hpo_uses_official_openai_api(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({
                    "choices": [{"message": {"content": json.dumps({
                        "phenotypes": [{"hpo_id": "HP:0000001", "evidence": "reported symptom"}]
                    })}}],
                }).encode()

        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-key",
            "OPENAI_MODEL": "gpt-4o-mini",
        }, clear=True), patch("meddeck.urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            results, error = extract_hpo("reported symptom", {"HP:0000001": "Test phenotype"})

        self.assertIsNone(error)
        self.assertEqual(results[0]["id"], "HP:0000001")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(json.loads(request.data)["model"], "gpt-4o-mini")

    def test_extract_hpo_requires_openai_api_key(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            results, error = extract_hpo("reported symptom", {"HP:0000001": "Test phenotype"})
        self.assertEqual(results, [])
        self.assertIn("run_meddeck.ps1", error)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "unit-test-secret",
            "DATABASE": str(Path(self.temp_dir.name) / "test.sqlite"),
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def token(self):
        with self.client.session_transaction() as sess:
            return sess["csrf_token"]

    def test_pages_render_and_csrf_required(self):
        for path in ("/", "/atlas", "/sources", "/register", "/login"):
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.post("/login").status_code, 400)

    def test_csrf_session_remains_valid_after_app_restart(self):
        secret_file = Path(self.temp_dir.name) / "instance" / "flask-secret.key"
        database_path = str(Path(self.temp_dir.name) / "restart.sqlite")
        first_app = create_app({
            "TESTING": True,
            "SECRET_KEY_FILE": str(secret_file),
            "DATABASE": database_path,
        })
        first_client = first_app.test_client()
        first_client.get("/login")
        with first_client.session_transaction() as browser_session:
            token = browser_session["csrf_token"]
        session_cookie = first_client.get_cookie("session")
        self.assertTrue(secret_file.is_file())

        restarted_app = create_app({
            "TESTING": True,
            "SECRET_KEY_FILE": str(secret_file),
            "DATABASE": database_path,
        })
        restarted_client = restarted_app.test_client()
        restarted_client.set_cookie("session", session_cookie.value)
        response = restarted_client.post("/login", data={
            "csrf_token": token, "email": "missing@example.org", "password": "wrong-password",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("Incorrect email or password.".encode(), response.data)

    def test_registration_login_and_private_dashboard(self):
        self.client.get("/register")
        token = self.token()
        response = self.client.post("/register", data={
            "csrf_token": token, "name": "Test Person", "email": "patient@example.org",
            "password": "not-a-real-password", "age": "31", "sex": "prefer_not",
            "role": "patient", "diagnosed": "no", "symptoms": "example symptom",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Hello, Test Person", response.data)
        self.client.post("/logout", data={"csrf_token": self.token()})
        self.client.get("/login")
        response = self.client.post("/login", data={
            "csrf_token": self.token(), "email": "patient@example.org",
            "password": "not-a-real-password",
        }, follow_redirects=True)
        self.assertIn(b"Hello, Test Person", response.data)

    def test_user_can_revoke_ai_consent_and_delete_account(self):
        self.client.get("/register")
        self.client.post("/register", data={
            "csrf_token": self.token(), "name": "Test Person", "email": "delete@example.org",
            "password": "not-a-real-password", "age": "31", "sex": "prefer_not",
            "role": "patient", "diagnosed": "no", "symptoms": "example symptom",
            "consent_ai": "yes", "consent_gene_graph": "yes",
        })
        self.client.get("/profile")
        self.client.post("/profile", data={
            "csrf_token": self.token(), "name": "Test Person", "condition": "",
            "symptoms": "example symptom",
        })
        with self.app.test_client() as client:
            from meddeck import database
            with database(self.app.config["DATABASE"]) as db:
                user = db.execute("SELECT consent_ai FROM users WHERE email=?", ("delete@example.org",)).fetchone()
            self.assertEqual(user["consent_ai"], 0)
            graph = self.client.get("/gene-graph")
            self.assertNotIn(b"Test Person", graph.data)
        self.client.post("/delete-account", data={
            "csrf_token": self.token(), "confirmation": "DELETE",
        })
        with self.app.app_context():
            from meddeck import database
            with database(self.app.config["DATABASE"]) as db:
                self.assertIsNone(db.execute("SELECT id FROM users WHERE email=?", ("delete@example.org",)).fetchone())

    def test_gene_graph_shows_only_opted_in_patients_and_real_names(self):
        self.client.get("/register")
        self.client.post("/register", data={
            "csrf_token": self.token(), "name": "Visible Patient", "email": "visible@example.org",
            "password": "not-a-real-password", "age": "31", "sex": "prefer_not",
            "role": "patient", "diagnosed": "no", "symptoms": "example symptom",
            "consent_gene_graph": "yes",
        })
        from meddeck import database
        with database(self.app.config["DATABASE"]) as db:
            db.execute("UPDATE users SET gene_candidates_json=? WHERE email=?",
                       (json.dumps([{"symbol": "AARS1", "matched": ["HP:0000001"]}]),
                        "visible@example.org"))
        response = self.client.get("/gene-graph")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Visible Patient", response.data)
        self.assertIn(b"AARS1", response.data)
        self.assertIn('action="/join-candidate-gene"', response.data.decode())
        other = self.app.test_client()
        other.get("/register")
        with other.session_transaction() as session:
            other_token = session["csrf_token"]
        other.post("/register", data={
            "csrf_token": other_token, "name": "Private Patient", "email": "private@example.org",
            "password": "not-a-real-password", "age": "31", "sex": "prefer_not",
            "role": "patient", "diagnosed": "no", "symptoms": "example symptom",
        })
        with database(self.app.config["DATABASE"]) as db:
            db.execute("UPDATE users SET gene_candidates_json=? WHERE email=?",
                       (json.dumps([{"symbol": "GENE2", "matched": ["HP:0000002"]}]),
                        "private@example.org"))
        response = self.client.get("/gene-graph")
        self.assertIn(b"Visible Patient", response.data)
        self.assertNotIn(b"Private Patient", response.data)
        self.assertNotIn(b"GENE2", response.data)

    def test_gene_graph_requires_login(self):
        response = self.client.get("/gene-graph")
        self.assertEqual(response.status_code, 302)

    def test_dashboard_shows_openai_configuration_from_process_environment(self):
        self.client.get("/register")
        self.client.post("/register", data={
            "csrf_token": self.token(), "name": "Test Patient", "email": "config@example.org",
            "password": "not-a-real-password", "age": "31", "sex": "prefer_not",
            "role": "patient", "diagnosed": "no", "symptoms": "example symptom",
        })
        hpo_data = (
            {"HP:0000001": "Test phenotype"},
            {("OMIM:1", "Test condition"): {"HP:0000001"}},
            {},
        )
        with patch("meddeck.load_hpo_data", return_value=hpo_data), patch.dict(
            os.environ, {"OPENAI_API_KEY": ""}
        ):
            response = self.client.get("/dashboard")
            self.assertIn("AI is not configured".encode(), response.data)
        with patch("meddeck.load_hpo_data", return_value=hpo_data), patch.dict(
            os.environ, {"OPENAI_API_KEY": "test-key"}
        ):
            response = self.client.get("/dashboard")
            self.assertIn("AI is configured".encode(), response.data)

    def test_symptom_analysis_saves_gene_candidates_and_joins_community(self):
        self.client.get("/register")
        self.client.post("/register", data={
            "csrf_token": self.token(), "name": "Candidate Patient", "email": "candidate@example.org",
            "password": "not-a-real-password", "age": "31", "sex": "prefer_not",
            "role": "patient", "diagnosed": "no", "symptoms": "example symptom",
            "consent_ai": "yes", "consent_gene_graph": "yes",
        })
        self.client.get("/dashboard")
        with patch("meddeck.load_hpo_data", return_value=(
            {"HP:0000001": "Test phenotype"},
            {("OMIM:1", "Test condition"): {"HP:0000001"}},
            {"GENE1": {"HP:0000001"}},
        )), patch("meddeck.extract_hpo", return_value=(
            [{"id": "HP:0000001", "label": "Test phenotype", "evidence": "example symptom"}], None
        )):
            response = self.client.post("/analyze", data={"csrf_token": self.token()}, follow_redirects=True)
            self.assertIn(b"GENE1", response.data)
            self.assertIn(b"PATIENT HPO REPORT", response.data)
            self.assertIn(b"HP:0000001", response.data)
            self.assertIn(b"Test phenotype", response.data)
            self.assertIn(b"Evidence: example symptom", response.data)
            graph = self.client.get("/gene-graph")
            self.assertIn(b"Candidate Patient", graph.data)
            self.assertIn(b"GENE1", graph.data)
        from meddeck import database
        with database(self.app.config["DATABASE"]) as db:
            user = db.execute(
                "SELECT gene_candidates_json FROM users WHERE email=?", ("candidate@example.org",)
            ).fetchone()
            membership = db.execute(
                """SELECT 1 FROM memberships m JOIN communities c ON c.id=m.community_id
                   JOIN users u ON u.id=m.user_id
                   WHERE u.email=? AND c.name='GENE1' AND c.basis='candidate_gene'""",
                ("candidate@example.org",),
            ).fetchone()
        self.assertEqual(json.loads(user["gene_candidates_json"])[0]["symbol"], "GENE1")
        self.assertIsNotNone(membership)

    def test_load_dotenv_file(self):
        from meddeck import load_dotenv_file
        with tempfile.NamedTemporaryFile(mode="w", delete=False, encoding="utf-8") as f:
            f.write("TEST_ENV_MEDDECK_KEY=secret-12345\n# comment\nOTHER_KEY=\"hello world\"\n")
            temp_env_path = f.name
        try:
            load_dotenv_file(temp_env_path)
            self.assertEqual(os.environ.get("TEST_ENV_MEDDECK_KEY"), "secret-12345")
            self.assertEqual(os.environ.get("OTHER_KEY"), "hello world")
        finally:
            if os.path.exists(temp_env_path):
                os.remove(temp_env_path)

    def test_community_and_patient_graphs(self):
        self.client.get("/register")
        self.client.post("/register", data={
            "csrf_token": self.token(), "name": "Graph Member", "email": "graphmember@example.org",
            "password": "not-a-real-password", "age": "28", "sex": "female",
            "role": "patient", "diagnosed": "yes", "condition": "Rare Syndrome", "symptoms": "pain",
            "consent_gene_graph": "yes",
        })
        resp = self.client.get("/dashboard")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"MeddeckGraph", resp.data)

        # Test community view
        from meddeck import database
        with database(self.app.config["DATABASE"]) as db:
            comm = db.execute("SELECT id FROM communities WHERE name='Rare Syndrome'").fetchone()
        if comm:
            comm_resp = self.client.get(f"/community/{comm['id']}")
            self.assertEqual(comm_resp.status_code, 200)
            self.assertIn(b"community-graph", comm_resp.data)
            self.assertIn(b"MeddeckGraph", comm_resp.data)


if __name__ == "__main__":
    unittest.main()
