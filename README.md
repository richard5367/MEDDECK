# MEDDECK

Prototipo web local en Python para conectar personas, explorar relaciones enfermedad-fenotipo y compartir fuentes biomédicas. Está construido con Flask y SQLite. **No es un sistema de diagnóstico ni está listo para alojar datos de salud reales en un servidor público.**

## Ejecutar en Windows

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python meddeck.py
```

Abre http://127.0.0.1:5000. La base de datos se crea en `instance\meddeck.sqlite`; la clave de sesión local se crea una sola vez en `instance\flask-secret.key` y se conserva entre reinicios para que no expiren formularios y sesiones. No borres ese archivo mientras uses la app. En un entorno desplegado, define `MEDDECK_SECRET_KEY` como un secreto estable en el proveedor y no uses `MEDDECK_DEBUG=1`.

## Datos HPO y análisis de síntomas

El atlas no incluye asociaciones médicas ficticias. MEDDECK detecta en Descargas `hp.obo`, `phenotype.hpoa` y el archivo oficial [genes_to_phenotype.txt](https://github.com/obophenotype/human-phenotype-ontology/releases/download/v2026-09-01/genes_to_phenotype.txt). Si mueves o renombras estos ficheros, indica las rutas con `HPO_OBO_PATH`, `HPOA_PATH` y `HPO_GENE_PATH`. Revisa fecha, atribución y términos de uso de los archivos publicados por [HPO](https://github.com/obophenotype/human-phenotype-ontology/releases).

```powershell
.\run_meddeck.ps1
```

El lanzador pide la clave con entrada oculta y la entrega al proceso de MEDDECK sin escribirla en código, `.env`, historial del terminal ni base de datos. El valor se elimina del entorno al salir del servidor. Por defecto usa `gpt-4o-mini` y el puerto `5009`. Si PowerShell bloquea la ejecución del script, ejecuta primero `Set-ExecutionPolicy -Scope Process Bypass` solo en esa ventana y vuelve a ejecutar el lanzador. Detén el servidor anterior con `Ctrl+C` y arráncalo con este script; cambiar variables en otra terminal no modifica un proceso Flask que ya está corriendo.

Si una clave se compartió en el chat, revócala desde la plataforma de OpenAI y crea otra antes de usar el lanzador. El análisis usa exclusivamente la API oficial de OpenAI.

OpenAI se usa solo cuando la persona marcó consentimiento; se envía el texto de síntomas, no nombre ni correo. El uso de la API puede tener costo y depende de tu cuenta, límites y políticas de OpenAI. Sin clave, archivos de ontología o consentimiento, no se procesan síntomas. Los identificadores propuestos se validan contra la ontología local. Luego los fenotipos HPO se comparan con genes asociados por HPO; el resultado son **genes candidatos relacionados con fenotipos**, no una inferencia de genes afectados ni de variantes presentes. Los grupos asociados se crean como espacios de conversación. La similitud de enfermedad no es probabilidad, diagnóstico ni consejo médico. Toda salida requiere revisión clínica.

El grafo global de genes solo muestra por nombre a pacientes que activaron el consentimiento específico desde registro o perfil; muestra el nombre y sus genes candidatos, nunca el correo ni síntomas. Cualquier usuario con cuenta puede consultar el grafo y unirse a comunidades de conversación. El consentimiento se puede retirar desde Mi perfil.

HPOA puede incluir registros identificados por OMIM u otras fuentes; no se accede a una API de OMIM ni se redistribuyen sus datos. El acceso a OMIM puede requerir autorización y está sujeto a sus condiciones. PubMed, Orphanet, MONDO, ClinVar, ClinicalTrials.gov y recursos de organizaciones se ofrecen como enlaces a las fuentes originales. Esta versión no extrae automáticamente sus catálogos.

## Lo que ya permite probar

- Registro e inicio de sesión con contraseñas con hash; roles paciente, médico e investigador.
- Formularios adaptados al rol, consentimiento revocable para análisis con IA y para aparecer por nombre en el grafo, edición y borrado de perfil, y protección CSRF.
- Comunidades por condición para personas diagnosticadas/profesionales; comunidades por gen al actualizar el diagnóstico; mensajes y opción de salir.
- Extracción opcional de HPO y ordenamiento ponderado de coincidencias a partir de datos HPO configurados.
- Genes candidatos basados en asociaciones HPO oficiales, comunidades de discusión y grafo global por consentimiento.
- Atlas de relaciones enfermedad-fenotipo y directorio de fuentes externas.

Esta demostración limita el registro a mayores de edad; todavía no tiene consentimiento de tutores ni un flujo para cuentas de menores.

## Antes de un despliegue público

Hace falta un despliegue administrado (ningún proveedor/credencial de alojamiento fue indicado), HTTPS, `MEDDECK_SECRET_KEY` estable y secreto, base de datos con controles de acceso y cifrado, copias de seguridad y retención/borrado, moderación y herramientas de denuncia, proceso de consentimiento y privacidad conforme a la jurisdicción, evaluación clínica del algoritmo, accesibilidad y pruebas de seguridad. La base SQLite y este prototipo no son adecuados para alojar datos de salud reales expuestos a internet. No publiques el servidor de desarrollo de Flask ni guardes claves de proveedor en el código.

## Pruebas

```powershell
python -m unittest discover -s tests -v
```
