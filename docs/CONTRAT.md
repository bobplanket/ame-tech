# Contrat partagé : logiciels AME Tech (dépôt public bobplanket/ame-tech)

Ce dépôt est **public**. Rien de secret n'y entre jamais : ni secret TOTP, ni mot de passe, ni clé privée, ni jeton de lecture OBS, ni le code fixe de Marcel. Les secrets vivent sur chaque poste (DPAPI Windows) ou sur le VPS.

Spec : `~/clawd/badaboum/ame-tech/DESIGN.md` (sections 3, 5, 6). Code de référence du connecteur actuel : `~/clawd/badaboum/badaboum-propresenter-connecteur/` (`connecteur_v2.py`, `passerelle.py`, `windows-runtime/`), à lire et réutiliser.

Serveur cible : tech.ame.paris (code dans `~/clawd/badaboum/badaboum-live-v2/server_v2.py`, mode `--auth totp`). Accès écriture : cookie `badaboum_session` obtenu par `POST /api/login {"code": "<6 chiffres>"}` (12 h), ou en-tête `X-Code: <code du moment>` à chaque requête. Routes du relais : `POST /api/presenter/claim {name, client_id}` → `{session, epoch, server_time, lease}` ; `POST /api/presenter/frame {session, sequence, sent_at, content}` (voir `connecteur_v2.py` et `test_faults.py` pour le format exact). `GET /api/heure` → `{t}` (public). `GET /api/session` → `{pin_requis, mode, theme}`.

Python 3.13 stdlib uniquement côté Windows (runtime embarqué, pas de pip). Textes visibles en français, sans tiret cadratin ni demi-cadratin.

## Arborescence et propriétaires

| Chemin | Propriétaire |
|---|---|
| `windows/ametech/__init__.py`, `version.py` (`VERSION = "3.0.1"`), `totp.py`, `propresenter.py`, `cible.py`, `relais.py`, `journal.py`, `coffre.py`, `ui_server.py`, `__main__.py`, `tests/test_relais*.py`, `tests/faux_propresenter.py` | Agent **relais** |
| `windows/ametech/www/` (index.html, app.css, app.js, assets/) et `windows/outils/maquette_api.py` (faux serveur local pour développer l'UI) | Agent **interface** |
| `windows/ametech/maj.py`, `windows/ametech/ed25519.py`, `windows/ametech/demarrage.py`, `windows/ametech/cle_publique.txt`, `windows/runtime/`, `windows/build_zip.py`, `.github/workflows/`, `outils/signer_manifeste.py`, `outils/generer_cles.py`, `tests/test_maj*.py` | Agent **paquet** |
| `windows/ametech/assistance.py`, `tests/test_assistance.py`, `docs/RUSTDESK.md` | Agent **assistance** |
| `obs/ame-tech.lua`, `obs/LISEZMOI.md` | Agent **obs** |
| `README.md`, `docs/CONTRAT.md` | Claude (intégration) |

Un agent ne crée ni ne modifie de fichier hors de ses chemins, ne fait ni `git commit`, ni `git switch`, ni `push`. L'intégrateur commite.

## Modules et signatures (Python, package `ametech`)

Tous les modules s'importent par `from ametech import x`. Le point d'entrée est `python -m ametech` (lancé par le lanceur Windows avec `pythonw.exe`).

### `coffre.py` (relais)
- `lire() -> dict` / `ecrire(d: dict) -> None` : réglages persistants dans `%APPDATA%\AME Tech\reglages.json`. Les champs secrets (`secret_totp`) sont chiffrés DPAPI (`CryptProtectData`, voir `protect()` de `connecteur_v2.py`) ; hors Windows (tests, Mac) chiffrement absent mais fichier en 600.
- Clés connues : `secret_totp` (base32), `cibles` (liste d'URL, défaut `["https://tech.ame.paris"]`), `port_pp` (int, 0 = auto), `mode` (`"auto"|"manuel"`), `demarrage_windows` (bool), `relais_au_lancement` (bool), `relais_apres_maj` (bool, interne : posé par l'arrêt pour relance quand le relais tournait, effacé après son redémarrage), `canal_maj` (`"stable"`).

### `totp.py` (relais)
Copie conforme de `~/clawd/badaboum/badaboum-live-v2/totp.py` (mêmes fonctions : `code`, `verifier`, `lire_enrolement`, ...).

### `journal.py` (relais)
- `log(niveau: str, etape: str, message: str, **details) -> None` ; niveaux `info|alerte|erreur`. Anneau mémoire de 2000 lignes + fichier tournant `%APPDATA%\AME Tech\journal.log` (1 Mo × 3).
- `dernieres(n: int = 500) -> list[dict]` ; `rapport_zip() -> bytes` (journal + diagnostic, **sans secret** : le secret TOTP, les cookies et les codes sont masqués).

### `relais.py` (relais)
- `class Relais` : `demarrer()`, `arreter()`, `etat() -> dict`, `definir_mode(mode: str)`, `diagnostic() -> dict`.
- Détection auto du processus et du port ProPresenter (reprendre `windows_ports()` / `discover()`), reconnexion progressive, bail serveur, aucune file de vieux textes, redondance : essaie les `cibles` dans l'ordre, bascule sur la suivante après 3 échecs consécutifs, revient à la première toutes les 5 min.
- Auth cible : login TOTP (`cible.py`) puis cookie ; renouvellement anticipé avec le code du moment (dès 11 h si aucun texte depuis 1 min, au plus tard à 11 h 30), nouveau login sur 401.
- Protection de l'adresse IP de l'église (le serveur la bloque après 8 codes refusés en 15 min) : après 2 refus consécutifs du code, plus aucun login, phase `erreur` « Code refusé deux fois : réenrôlez ce poste (code fourni par l'administrateur) », jusqu'à un nouvel enrôlement ou un redémarrage manuel du relais ; après un 429, aucun login pendant 15 min ; `diagnostic()` n'envoie pas de code pendant un refus.
- Mode choisi relais arrêté : poussé au serveur au démarrage du relais.

### `ui_server.py` (relais) : API locale consommée par l'interface

Écoute **127.0.0.1** seulement, port 48690 (repli 48691-48699). Ouvre la fenêtre : `msedge --app=http://127.0.0.1:<port>/ --window-size=980,760` (repli : navigateur par défaut). Jeton anti-CSRF : la page reçoit `window.AMETECH_JETON` injecté dans `index.html` au service, et chaque requête POST porte l'en-tête `X-Jeton`. Instance unique : un second lancement rouvre la fenêtre de l'instance active.

Toutes les réponses sont JSON, `Cache-Control: no-store`.

| Méthode, route | Corps | Réponse |
|---|---|---|
| `GET /api/etat` | | `{"version", "relais": {"actif": bool, "phase": "arret|recherche_pp|connexion|en_ligne|erreur", "message", "pp": {"port", "version", "nom"} \| null, "cible": {"url", "connectee": bool}, "dernier_texte", "depuis"}, "mode": "auto|manuel", "enrole": bool, "horloge": {"ecart_s": float \| null, "ok": bool}, "maj": <maj.etat()>, "assistance": <assistance.etat()>}` |
| `GET /api/code` | | `{"code": "123456", "reste_s": 17}` (403 si non enrôlé) |
| `POST /api/enroler` | `{"texte": "AMETECH1:..." ou URI otpauth}` | `{"ok": true}` ; 400 `{"erreur"}` |
| `POST /api/oublier` | | `{"ok": true}` (efface le secret) |
| `POST /api/relais/demarrer` / `POST /api/relais/arreter` | | `{"ok": true}` |
| `POST /api/mode` | `{"mode": "auto"\|"manuel"}` | `{"ok": true}` |
| `GET /api/journal?depuis=<n>` | | `{"lignes": [{"n", "t", "niveau", "etape", "message"}], "dernier": n}` |
| `GET /api/diagnostic` | | `{"etapes": [{"nom", "ok": bool, "detail"}]}` : ProPresenter, Internet, serveur, session, horloge |
| `GET /api/rapport` | | ZIP en téléchargement (`journal.rapport_zip()`) |
| `GET /api/pp/info` | | infos ProPresenter (version, port, couches actives) |
| `POST /api/pp/requete` | `{"methode": "GET"\|"POST"\|"PUT", "chemin": "/v1/...", "corps": any}` | `{"statut", "corps"}` (chemin limité à `/v1/` ; uniquement vers 127.0.0.1:<port PP>) |
| `GET /api/pp/flux` | | SSE des événements ProPresenter bruts (pour la molette technique) |
| `GET /api/reglages` / `POST /api/reglages` | sous-ensemble de `coffre` sans secret : `cibles`, `port_pp`, `demarrage_windows`, `relais_au_lancement` | `{...}` |
| `POST /api/maj/verifier` / `POST /api/maj/appliquer` | | `maj.verifier()` / `maj.appliquer()` |
| `POST /api/assistance/rustdesk` | | `assistance.lancer_rustdesk()` |
| `POST /api/quitter` | | `{"ok": true}` puis arrêt propre |

### `maj.py` (paquet)
- `etat() -> {"version_locale", "version_dispo": str|None, "statut": "a_jour|disponible|telechargement|pret|erreur|inconnu", "message", "verifie_le": float|None}`
- `verifier() -> dict` (même forme) ; `appliquer() -> dict` ; `boucle(relais) -> None` (thread : vérifie au lancement puis toutes les 6 h ; applique seulement si `relais.etat()["relais"]["phase"] == "arret"` ou si rien n'est transmis depuis 10 min : dernier `dernier_envoi`, à défaut `phase_depuis`). Le relais qui tournait repart après la relance (code de sortie 75, garanti par `__main__`).
- Source : `https://github.com/bobplanket/ame-tech/releases/latest/download/manifest.json` + `manifest.json.sig`, repli `https://tech.ame.paris/maj/manifest.json` (+ `.sig`).
- Manifeste : `{"version": "3.0.1", "date": "...", "notes": "...", "fichiers": [{"chemin": "ametech/relais.py", "sha256": "...", "url": "..."}], "zip": {"url": ".../AME-Tech-Windows.zip", "sha256": "..."}}`. Signature Ed25519 détachée (64 octets, base64) du fichier `manifest.json` tel quel ; clé publique dans `cle_publique.txt` (base64 32 octets). Refus total si la signature, une empreinte ou la version (strictement supérieure) ne colle pas.
- Application : télécharge le ZIP, vérifie, extrait dans `%LOCALAPPDATA%\AME Tech\versions\<version>\`, bascule le pointeur `courante.txt`, relance ; si la nouvelle version ne répond pas sur `/api/etat` en 30 s, retour à la précédente.

### `assistance.py` (assistance)
- `etat() -> {"rustdesk": "absent|pret|lance", "id": str|None, "serveur": "tech.ame.paris"}`
- `lancer_rustdesk() -> dict` : télécharge RustDesk (release officielle épinglée, empreinte SHA-256 en dur) dans `%LOCALAPPDATA%\AME Tech\rustdesk\` au premier appel, le lance configuré sur notre serveur (`tech.ame.paris`, clé publique hbbs en dur dans le module), renvoie l'identifiant à dicter.
- `CONTACT = {"nom": "Will Hardy", "whatsapp": "+1 (438) 458-2683", "wa_me": "https://wa.me/14384582683", "courriel": "bonjour@tmgconseil.fr"}`
- `TEAMVIEWER_URL = "https://www.teamviewer.com/fr/telechargement/windows/"` (lien officiel QuickSupport, jamais redistribué).

### `demarrage.py` (paquet)
- `activer(bool)` : raccourci dans le dossier Démarrage de l'utilisateur (pas de droits admin) ; `actif() -> bool`.

## Interface (agent interface)

Une seule page `www/index.html` + `app.css` + `app.js`, sans framework ni CDN (tout local, la fenêtre doit marcher hors ligne). Habillage AME (bleu nuit, bleu roi #1f5fb8, orange #f7931e, or #f6c56b, police Oswald pour les titres : `~/clawd/badaboum/badaboum-live-v2/www/fonts/oswald-latin.woff2`, logo `~/clawd/badaboum/badaboum-live-v2/www/assets/ame/logo-ame.png`, copiés dans `www/assets/`). Contenu : état du relais en gros (vert / orange / rouge), boutons **Démarrer le relais ProPresenter** et **Arrêter**, bascule **Auto / Manuel**, code à 6 chiffres en très grand avec barre des 30 s et bouton copier, écran d'enrôlement si `enrole=false`, onglet **Débug** (journal en direct, diagnostic, export du rapport), **molette technique** (icône engrenage) ouvrant le panneau API ProPresenter (infos, requête brute, flux d'événements, réglages), bloc **Assistance** (« En cas d'assistance, contacter Will Hardy : WhatsApp +1 (438) 458-2683 ou bonjour@tmgconseil.fr », QR WhatsApp vers `https://wa.me/14384582683` en SVG statique, bouton Assistance à distance RustDesk, lien TeamViewer QuickSupport), état des mises à jour avec bouton Vérifier. Largeur cible 980×760, utilisable à 720 px.

## OBS (agent obs)

Script Lua `obs/ame-tech.lua` (OBS 30+, Mac et Windows, `obslua`). Réglages du script : URL du site (défaut `https://tech.ame.paris` ; `http://` refusé hors `localhost` et `127.0.0.1`, avec un message dans l'état du script, car le jeton de lecture partirait en clair), lien Habillage (collé depuis la page Sources OBS du site, contient le jeton de lecture : stocké dans les réglages OBS, jamais dans le script). Fonctions : créer la scène « Habillage » et sa source navigateur (1920×1080, `shutdown=false`, `restart_when_active=false`, `reroute_audio=true`) ; bouton et raccourci « Rafraîchir l'habillage » (vide le cache de toutes les sources navigateur dont l'URL commence par l'URL du site : `obs_properties_get` + `obs_property_button_clicked` sur `refreshnocache`) ; rafraîchissement auto quand `GET <site>/api/version` change (sondage toutes les 30 s via la source navigateur cachée ou `os.execute curl` ; seulement si `GET <site>/api/etat?view=<jeton>` montre que rien n'est à l'antenne : `lt, verset, chant, carton, bandeau, don, donner, whatsapp, media, plein` tous `on=false`, une section absente comptant comme allumée) ; raccourcis : tout masquer, logo, bandeau, bascule Auto / Manuel (ces actions d'écriture passent par le panneau dock, pas par le script, car le script n'a pas de code d'écriture). Le script affiche l'adresse du panneau à ajouter en dock : `<site>/panneau.html`.
