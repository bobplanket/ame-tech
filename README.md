# AME Tech

Logiciels de régie de l'église AME Paris, reliés à [tech.ame.paris](https://tech.ame.paris) :

- **AME Tech pour Windows** (`windows/`) : relais ProPresenter vers la régie (détection automatique, reconnexion, cibles de secours, Auto / Manuel), code d'accès à 6 chiffres calculé hors ligne, débug complet, panneau technique de l'API ProPresenter, assistance à distance RustDesk, mises à jour automatiques signées.
- **Script OBS** (`obs/ame-tech.lua`) : scène Habillage, rafraîchissement de l'habillage, panneau de régie en dock.

## Installer

Télécharger [AME-Tech-Windows.zip](https://github.com/bobplanket/ame-tech/releases/latest/download/AME-Tech-Windows.zip), extraire, lancer `AME Tech.exe`, coller le code d'enrôlement fourni par l'administrateur, puis **Démarrer le relais ProPresenter**. Le script OBS : [ame-tech.lua](https://github.com/bobplanket/ame-tech/releases/latest/download/ame-tech.lua), voir `obs/LISEZMOI.md`.

## Sécurité

Aucun secret dans ce dépôt. Le secret du code d'accès est enrôlé sur chaque poste (coffre Windows DPAPI). Chaque mise à jour est un manifeste signé Ed25519 ; le logiciel refuse tout fichier dont la signature ou l'empreinte ne correspond pas (`windows/ametech/cle_publique.txt`).

## Publier une version

Changer `VERSION` dans `windows/ametech/version.py`, puis pousser le tag `v<VERSION>`. GitHub Actions teste, construit, signe et publie ; les postes se mettent à jour seuls quand rien n'est transmis à l'antenne.

## Assistance

Will Hardy : WhatsApp +1 (438) 458-2683, bonjour@tmgconseil.fr.
