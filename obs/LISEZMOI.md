# AME Tech pour OBS

Un script Lua pour OBS 30 ou plus récent, sur Mac comme sur Windows. Lua est intégré à OBS : rien à installer. Le script crée la scène Habillage, rafraîchit les sources du site d'un clic ou d'un raccourci, le fait aussi tout seul quand le site publie une nouvelle version, et donne l'adresse du panneau de régie à ouvrir dans un dock.

## Installation en 5 étapes

1. **Récupérer le script.** Enregistrez `ame-tech.lua` dans un dossier qui ne bougera plus (par exemple `Documents/OBS`). OBS le relit à chaque démarrage depuis cet endroit.
2. **L'ajouter à OBS.** Menu **Outils > Scripts**, bouton **+**, choisissez `ame-tech.lua`. Le script apparaît dans la liste avec ses réglages à droite.
3. **Coller le lien Habillage.** Sur le site (`https://tech.ame.paris`), connectez-vous avec le code à 6 chiffres, ouvrez **Sources OBS** et cliquez **Copier le lien** sur la ligne « Habillage ». Collez-le dans le champ **Lien Habillage** du script. Laissez l'adresse du site telle quelle, sauf consigne contraire.
4. **Créer la scène.** Cliquez **Créer la scène Habillage**. La scène « Habillage » arrive avec sa source navigateur « Habillage AME » (1920 x 1080, toujours chargée, son géré par OBS). Dans chaque scène de direct, ajoutez-la par-dessus la caméra : **Sources > + > Scène > Habillage**. Un second clic ne crée pas de doublon : il remet seulement les bons réglages.
5. **Ajouter le panneau en dock.** Menu **Docks > Docks de navigateur personnalisés**, nom « AME Panneau », adresse `https://tech.ame.paris/panneau.html` (affichée aussi dans les réglages du script, ligne « Panneau (dock) »), puis **Appliquer**. Le dock demande le code à 6 chiffres une fois toutes les 12 heures et revient ensuite au panneau.

Pour un raccourci clavier : **Paramètres > Raccourcis clavier**, cherchez « Rafraîchir l'habillage (AME Tech) ».

## Ce que fait le script

- **Rafraîchir l'habillage** (bouton et raccourci) : vide le cache de toutes les sources navigateur dont l'adresse commence par celle du site. Plus besoin d'ouvrir les propriétés de chaque source.
- **Rafraîchissement automatique** : toutes les 30 secondes, le script lit la version du site (`/api/version`). Quand elle change, il vérifie l'état de l'antenne avec le jeton du lien Habillage (`/api/etat?view=...`). Il ne rafraîchit que si rien n'est affiché : noms, versets, chants, cartons, bandeau, encarts de don, média et écrans plein cadre tous éteints. Sinon il attend et réessaie au sondage suivant.
- **Ligne « État »** dans les réglages du script : dernière version vue, rafraîchissement en attente, jeton refusé, site injoignable. Le détail part aussi dans **Outils > Scripts > Journal des scripts**.

Les requêtes passent par libcurl, déjà présente dans OBS (livrée avec OBS sous Windows, fournie par le système sur Mac). Le script l'appelle sans jamais attendre le réseau : OBS ne fige pas, même si le site ne répond pas. Pas de fenêtre de console sous Windows, pas de fichier temporaire, et le certificat HTTPS du site est vérifié. Si libcurl manquait, seul le rafraîchissement automatique serait coupé ; le bouton et le raccourci marchent toujours.

## Le panneau

Le panneau est une régie réduite, pensée pour un dock de 300 à 420 pixels de large : paroles **Auto** (ProPresenter) ou **Manuel**, bandeau avec choix du texte dans la bibliothèque, logo, écrans plein cadre (attente, pause, fin, technique, retour au direct) et **Tout masquer**. Il affiche l'état de la connexion au site et celui de ProPresenter, et suit l'habillage du moment (Badaboum ou AME).

Quand le dock a le focus, quatre touches agissent : **Échap** tout masquer, **A** Auto ou Manuel, **B** bandeau, **L** logo. Ces actions passent par le panneau et sa session : le script, lui, ne possède aucun code d'écriture.

## Sécurité

Le lien Habillage contient un jeton de lecture : il permet d'afficher l'habillage, pas de le piloter. Il reste dans les réglages d'OBS de ce poste et n'est jamais écrit dans le script ni dans le journal. Ne le publiez pas et ne l'envoyez pas hors de l'équipe technique. En cas de fuite, prévenez l'administrateur du site.

## Dépannage

- **« Rafraîchissement automatique indisponible »** : libcurl introuvable sur ce poste. Utilisez le bouton ou le raccourci et signalez-le.
- **« Jeton refusé »** : le lien collé n'est plus valable. Recopiez-le depuis la page Sources OBS.
- **« Nouvelle version en attente »** : quelque chose est à l'antenne. Le rafraîchissement se fera dès que tout sera masqué, ou tout de suite avec le bouton.
- **Le dock reste sur la page de connexion** : saisissez le code à 6 chiffres affiché par le logiciel AME Tech ou l'appli Code AME.
