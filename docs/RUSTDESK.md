# Assistance à distance RustDesk

Le bouton « Assistance à distance » d'AME Tech ouvre RustDesk sur le poste Windows de l'église, déjà réglé sur notre serveur. La personne sur place dicte un identifiant et un mot de passe, Will prend la main depuis son Mac. Aucun trafic ne passe par les serveurs publics de RustDesk.

## Serveur

| Élément | Valeur |
|---|---|
| Hôte | `tech.ame.paris` (45.155.169.77, VPS TMG-S2) |
| Dossier | `/opt/ame-tech-rustdesk/` (`compose.yaml`, `data/`) |
| Image | `rustdesk/rustdesk-server:1.1.16`, épinglée par empreinte dans `compose.yaml` |
| Conteneurs | `ame-rustdesk-hbbs` (identifiants, `hbbs -r tech.ame.paris:21117 -k _`) et `ame-rustdesk-hbbr` (relais, `hbbr -k _`) |
| Réseau | mode hôte, pour que UFW filtre réellement les ports (un port publié par Docker contournerait UFW) |
| Ports ouverts (UFW) | `21115:21119/tcp` et `21116/udp` |
| Redémarrage | `restart: unless-stopped` |
| Clé publique | `El2YXaFcYnktAqPPA8U4s1Ytro0MQRsI0+Arvni0td0=` |

L'option `-k _` refuse tout client qui ne présente pas cette clé : le serveur ne sert pas de relais ouvert. La clé privée (`data/id_ed25519`, droits 600) ne quitte jamais le serveur et n'entre jamais dans ce dépôt. La clé publique, elle, n'a rien de secret.

Commandes utiles sur le VPS :

```sh
cd /opt/ame-tech-rustdesk
docker compose ps
docker compose logs --tail 50
docker compose restart          # sans toucher aux autres services du VPS
cat data/id_ed25519.pub         # clé publique (jamais id_ed25519)
```

Changer de version : modifier le tag et l'empreinte (`docker pull`, puis `docker image inspect --format '{{index .RepoDigests 0}}'`), puis `docker compose up -d`. Ne jamais supprimer `data/` : une nouvelle paire de clés obligerait à republier le client.

## Client Windows (module `ametech/assistance.py`)

- Version épinglée : RustDesk **1.5.0**, asset officiel `rustdesk-1.5.0-x86_64.exe` de `github.com/rustdesk/rustdesk/releases`, 25 887 600 octets, SHA-256 `8555777215510d83d2d61c9dc984e4fcc838bd7e79f9d18a42585431f5e8bb47` (calculée sur le fichier téléchargé, identique à l'empreinte publiée par GitHub).
- Premier clic : téléchargement dans `%LOCALAPPDATA%\AME Tech\rustdesk\` via un fichier `.part`, contrôle de la taille et de l'empreinte, refus et suppression si elles diffèrent. L'empreinte est recontrôlée avant chaque lancement ; un fichier altéré est retéléchargé.
- Aucun droit administrateur, aucune installation : c'est la version portable.

### Pourquoi le renommage de l'exécutable

Le fichier est enregistré sous le nom :

```
rustdesk-host=tech.ame.paris,key=El2YXaFcYnktAqPPA8U4s1Ytro0MQRsI0+Arvni0td0=,.exe
```

RustDesk lit l'hôte et la clé dans son propre nom de fichier au démarrage (`src/custom_server.rs`, fonction `get_custom_server_from_string`). C'est le mécanisme documenté par RustDesk pour les clients préconfigurés, et le paquet portable transmet ce nom au programme qu'il extrait. La virgule finale protège la clé quand Windows ajoute « (1) » à un fichier en double.

L'autre voie, `rustdesk.exe --config <chaîne>`, exige dans la version 1.5.0 un RustDesk installé et lancé en administrateur (`src/core_main.rs`) : inutilisable pour un clic sans installation. Le renommage n'a qu'une limite : la clé ne doit pas contenir de `/`. La clé actuelle n'en contient pas, et un test échoue si une future clé en contenait.

### Identifiant du poste

Après le lancement, le module appelle `rustdesk --get-id` (sans droits admin en 1.5.0) toutes les 1,5 seconde pendant 30 secondes au plus, et garde la dernière ligne purement numérique (le paquet portable écrit ses propres lignes avant). L'identifiant est mémorisé dans `%LOCALAPPDATA%\AME Tech\rustdesk\id.txt` pour l'affichage. Passé ce délai, l'interface demande de lire l'identifiant dans la fenêtre RustDesk.

`lancer_rustdesk()` renvoie `{"rustdesk", "id", "serveur", "ok", "message"}` ; `message` est prêt à afficher.

## Se connecter depuis le Mac (Will)

1. Installer RustDesk pour macOS depuis la page officielle des releases (`rustdesk-<version>-aarch64.dmg` pour un Mac Apple Silicon, `x86_64` sinon). Accorder ensuite les autorisations demandées par macOS (Accessibilité, Enregistrement de l'écran) seulement si le Mac doit lui aussi être contrôlé ; pour prendre la main sur le poste de l'église, aucune n'est nécessaire.
2. Ouvrir RustDesk, menu « ⋮ » à côté de l'identifiant, puis **Réseau** (ou Réglages > Réseau), puis **Serveur ID/Relais** :
   - Serveur ID : `tech.ame.paris`
   - Serveur relais : `tech.ame.paris`
   - Serveur API : laisser vide
   - Clé : `El2YXaFcYnktAqPPA8U4s1Ytro0MQRsI0+Arvni0td0=`
3. Valider. En bas de la fenêtre, l'état doit indiquer « Prêt ».
4. Demander à la personne sur place de cliquer sur **Assistance à distance** dans AME Tech, puis de dicter l'identifiant (9 chiffres) et le mot de passe affiché dans la fenêtre RustDesk.
5. Saisir l'identifiant dans « Contrôler un bureau à distance », cliquer **Connecter**, puis entrer le mot de passe. La personne sur place peut aussi accepter la connexion d'un clic.

## Dépannage

| Symptôme | Piste |
|---|---|
| « L'empreinte du fichier RustDesk ne correspond pas » | Fichier corrompu ou intercepté (proxy, antivirus qui réécrit). Recliquer ; si l'erreur persiste, ne pas contourner, vérifier le réseau du poste. |
| « Téléchargement de RustDesk impossible » | Pas d'accès à `github.com` ou `objects.githubusercontent.com`. Repli : TeamViewer QuickSupport (lien dans le bloc Assistance). |
| Mac en « Pas prêt, vérifiez la connexion réseau » | Vérifier le réglage serveur et la clé (copier-coller exact, signe `=` final inclus). Depuis le Mac : `nc -vz tech.ame.paris 21116` et `nc -vz tech.ame.paris 21117`. |
| « Clé incorrecte » ou connexion refusée aussitôt | Le Mac et le poste n'utilisent pas la même clé, ou le Mac pointe encore vers les serveurs publics. |
| Identifiant non lu automatiquement | Le lire dans la fenêtre RustDesk (« Votre bureau »). Un pare-feu ou un antivirus peut retarder le premier lancement. |
| Connexion lente ou image figée | Le flux passe par le relais `21117/tcp` ; vérifier `docker compose logs hbbr` sur le VPS. |
| Rien ne répond côté serveur | Sur le VPS : `docker compose ps` dans `/opt/ame-tech-rustdesk`, puis `ufw status` (les deux règles RustDesk doivent figurer). |
| Remettre le poste à zéro | Fermer RustDesk, supprimer `%LOCALAPPDATA%\AME Tech\rustdesk\` : le prochain clic retélécharge et revérifie. |

## Non vérifié à la mise en place (01/11/2026)

La connexion réelle Windows vers Mac n'a pas été testée : seuls les ports (TCP 21115 à 21119 depuis le Mac, écoute UDP 21116 côté serveur) et la logique du module (tests unitaires sur Mac) l'ont été. Le premier essai doit confirmer que le paquet portable 1.5.0 renvoie bien l'identifiant sur `--get-id`.
