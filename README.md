# MEDDECK

A local Python web prototype for connecting people, exploring disease-phenotype relationships, and sharing biomedical resources. Built with Flask and SQLite. **This is not a diagnostic system and is not ready to host real health data on a public server.**

## Run on Windows

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python meddeck.py
```

Open http://127.0.0.1:5000. The database is created at `instance\meddeck.sqlite`; the local session key is created once at `instance\flask-secret.key` and retained across restarts so forms and sessions remain valid. Do not delete that file while using the app. In a deployed environment, set `MEDDECK_SECRET_KEY` to a stable secret in your hosting provider and do not enable `MEDDECK_DEBUG=1`.

## HPO data and symptom analysis

The atlas does not include fabricated medical associations. MEDDECK looks for `hp.obo`, `phenotype.hpoa`, and the official [genes_to_phenotype.txt](https://github.com/obophenotype/human-phenotype-ontology/releases/download/v2026-09-01/genes_to_phenotype.txt) in your Downloads folder. If you move or rename these files, set their paths with `HPO_OBO_PATH`, `HPOA_PATH`, and `HPO_GENE_PATH`. Check the date, attribution, and terms of use for files published by [HPO](https://github.com/obophenotype/human-phenotype-ontology/releases).

```powershell
.\run_meddeck.ps1
```

The launcher prompts for the key using hidden input and passes it to MEDDECK without writing it to source code, `.env`, terminal history, or the database. By default, the launcher uses the model configured in `.env` or `llama3.2`, and port `5000`. If PowerShell blocks script execution, run `Set-ExecutionPolicy -Scope Process Bypass` in that window, then run the launcher again. Stop any existing server with `Ctrl+C` and restart it with this script; changing variables in another terminal does not affect an already-running Flask process.

If a key was shared in chat, revoke it through your provider and create a new one before using the launcher. MEDDECK uses an OpenAI-compatible API; `OPENAI_BASE_URL` can point to a compatible provider.

The AI provider is used only when the person has given consent; only symptom text is sent, not name or email. API usage may incur costs and is subject to the provider's account, rate limits, and policies. Symptoms are not processed without a key, ontology files, and consent. Proposed identifiers are validated against the local ontology. The patient report lists each identified HPO term with its identifier, label, and supporting evidence. HPO phenotypes are then compared with HPO-associated genes; the results are **candidate genes associated with phenotypes**, not an inference that a patient has affected genes or variants. Related groups are created as conversation spaces. Disease similarity is not a probability, diagnosis, or medical advice. All output requires clinical review.

The global gene graph shows a patient's name and candidate genes only if they have given the specific consent in registration or their profile; it never shows their email or symptoms. Any registered user can view the graph and join conversation communities. Consent can be withdrawn from My Profile.

HPOA may include records identified by OMIM or other sources; MEDDECK does not access an OMIM API or redistribute its data. OMIM access may require authorization and is subject to its terms. PubMed, Orphanet, MONDO, ClinVar, ClinicalTrials.gov, and organization resources are provided as links to their original sources. This version does not automatically retrieve their catalogs.

## Features

- Account registration and sign-in with hashed passwords; patient, doctor, and researcher roles.
- Role-specific forms, revocable consent for AI analysis and name display in the graph, profile editing and deletion, and CSRF protection.
- Condition-based communities for people with a diagnosis and professionals; gene-based communities after diagnosis updates; messaging and the ability to leave.
- Optional HPO term extraction and weighted matching based on configured HPO data.
- Candidate genes based on official HPO associations, discussion communities, and a consent-based global graph.
- A disease-phenotype atlas and directory of external sources.

This demonstration limits registration to adults; it does not yet support guardian consent or accounts for minors.

## Before public deployment

A managed deployment (no hosting provider or credentials were specified), HTTPS, a stable secret `MEDDECK_SECRET_KEY`, a database with access controls and encryption, backups and retention/deletion policies, moderation and reporting tools, consent and privacy procedures appropriate to the jurisdiction, clinical evaluation of the algorithm, accessibility, and security testing are still needed. SQLite and this prototype are not suitable for hosting real health data exposed to the internet. Do not publish Flask's development server or store provider keys in source code.

## Tests

```powershell
python -m unittest discover -s tests -v
```
