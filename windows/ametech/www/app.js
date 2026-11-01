/* AME Tech : logique de la fenêtre. Aucun framework, aucune ressource externe.
   Consomme l'API locale de ui_server.py décrite dans docs/CONTRAT.md.
   Chaque POST porte l'en-tête X-Jeton (window.AMETECH_JETON injecté dans index.html). */
'use strict';
(() => {
  const JETON = String(window.AMETECH_JETON || '');
  if (!JETON || JETON.indexOf('{{') === 0) console.warn('AME Tech : jeton non injecté, les actions seront refusées.');

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const PHASES = {
    arret: 'Relais arrêté',
    recherche_pp: 'Recherche de ProPresenter',
    connexion: 'Connexion au serveur',
    en_ligne: 'En ligne',
    erreur: 'Erreur',
  };
  const MESSAGES_PAR_DEFAUT = {
    arret: 'Le site ne reçoit pas les textes de ProPresenter.',
    recherche_pp: 'Ouvrez ProPresenter sur ce PC : le relais le trouvera tout seul.',
    connexion: 'ProPresenter est trouvé, ouverture de la session sur le serveur.',
    en_ligne: 'Les textes de ProPresenter partent vers le site.',
    erreur: 'Le relais a rencontré un problème. Voir l\'onglet Débug.',
  };
  const STATUTS_MAJ = {
    a_jour: 'À jour',
    disponible: 'Mise à jour disponible',
    telechargement: 'Téléchargement en cours',
    pret: 'Prête à installer',
    erreur: 'Erreur',
    inconnu: 'Pas encore vérifié',
  };

  let etat = null;          // dernier /api/etat
  let echecsEtat = 0;
  let ferme = false;
  let rustdeskEnCours = false;
  let assistanceMessage = '';  // dernier message du module assistance, gardé entre deux sondages
  let majEnCours = false;
  let journalDernier = 0;
  let journalLignes = [];
  let erreursNonLues = 0;
  let premierJournal = true;
  let fluxSource = null;
  let codeAffiche = '';
  let codeReste = null;
  let reglagesCharges = false;

  /* ---------- Réseau ---------- */

  async function lire(chemin) {
    const r = await fetch(chemin, { cache: 'no-store', headers: { Accept: 'application/json' } });
    let d = null;
    try { d = await r.json(); } catch (_) { d = null; }
    if (!r.ok) {
      const e = new Error((d && (d.erreur || d.message)) || ('Erreur ' + r.status));
      e.statut = r.status;
      throw e;
    }
    return d;
  }

  async function envoyer(chemin, corps) {
    const r = await fetch(chemin, {
      method: 'POST',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-Jeton': JETON },
      body: JSON.stringify(corps === undefined ? {} : corps),
    });
    let d = null;
    try { d = await r.json(); } catch (_) { d = null; }
    if (!r.ok) {
      const e = new Error((d && (d.erreur || d.message)) || ('Erreur ' + r.status));
      e.statut = r.status;
      throw e;
    }
    return d;
  }

  /* ---------- Utilitaires ---------- */

  function annoncer(texte, erreur) {
    const zone = $('#annonce');
    const t = document.createElement('div');
    t.className = 'toast' + (erreur ? ' erreur' : '');
    t.textContent = texte;
    zone.appendChild(t);
    setTimeout(() => t.remove(), erreur ? 6000 : 3000);
  }

  function enDate(v) {
    if (v === null || v === undefined || v === '') return null;
    if (typeof v === 'number') return new Date(v > 1e12 ? v : v * 1000);
    const n = Number(v);
    if (!Number.isNaN(n) && String(v).trim() !== '') return new Date(n > 1e12 ? n : n * 1000);
    const d = new Date(v);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  function heure(d, secondes) {
    if (!d) return '';
    const p = (x) => String(x).padStart(2, '0');
    return p(d.getHours()) + ':' + p(d.getMinutes()) + (secondes ? ':' + p(d.getSeconds()) : '');
  }

  function duree(ms) {
    const s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return s + ' s';
    const m = Math.floor(s / 60);
    if (m < 60) return m + ' min';
    const h = Math.floor(m / 60);
    return h + ' h ' + String(m % 60).padStart(2, '0');
  }

  function versTexte(v) {
    if (v === null || v === undefined) return '';
    if (typeof v === 'string') return v;
    try { return JSON.stringify(v, null, 2); } catch (_) { return String(v); }
  }

  function hoteDe(url) {
    try { return new URL(url).host; } catch (_) { return String(url || ''); }
  }

  function confirmer(titre, texte, libelleOui, danger) {
    const d = $('#confirmation');
    $('#confirmation-titre').textContent = titre;
    $('#confirmation-texte').textContent = texte;
    const oui = $('#confirmation-oui');
    oui.textContent = libelleOui || 'Confirmer';
    oui.className = 'bouton ' + (danger === false ? 'bouton-principal' : 'bouton-danger');
    d.returnValue = '';
    return new Promise((resoudre) => {
      d.addEventListener('close', () => resoudre(d.returnValue === 'oui'), { once: true });
      d.showModal();
      $('#confirmation-non').focus();
    });
  }

  async function avecBouton(bouton, action) {
    if (bouton.disabled) return;
    bouton.disabled = true;
    bouton.setAttribute('aria-busy', 'true');
    try { return await action(); }
    finally { bouton.disabled = false; bouton.removeAttribute('aria-busy'); }
  }

  /* ---------- Onglets (clavier : flèches, Début, Fin) ---------- */

  function brancherOnglets(liste, surChangement) {
    const onglets = Array.from(liste.querySelectorAll('[role="tab"]'));
    function choisir(o, focus) {
      onglets.forEach((x) => {
        const actif = x === o;
        x.setAttribute('aria-selected', String(actif));
        x.tabIndex = actif ? 0 : -1;
        document.getElementById(x.getAttribute('aria-controls')).hidden = !actif;
      });
      if (focus) o.focus();
      if (surChangement) surChangement(o.id);
    }
    onglets.forEach((o, i) => {
      o.addEventListener('click', () => choisir(o, false));
      o.addEventListener('keydown', (e) => {
        let j = null;
        if (e.key === 'ArrowRight') j = (i + 1) % onglets.length;
        else if (e.key === 'ArrowLeft') j = (i - 1 + onglets.length) % onglets.length;
        else if (e.key === 'Home') j = 0;
        else if (e.key === 'End') j = onglets.length - 1;
        if (j !== null) { e.preventDefault(); choisir(onglets[j], true); }
      });
    });
    return choisir;
  }

  let ongletPrincipal = 'onglet-relais';
  brancherOnglets($('.entete .onglets'), (id) => {
    ongletPrincipal = id;
    if (id === 'onglet-debug') {
      erreursNonLues = 0;
      majPastille();
      chargerDiagnostic();
      defilerJournal(true);
    }
  });

  let ongletMolette = 'm-onglet-pp';
  brancherOnglets($('.onglets-molette'), (id) => {
    ongletMolette = id;
    if (id === 'm-onglet-reglages') chargerReglages();
    if (id === 'm-onglet-enrolement') rendreEnrolementMolette();
  });

  /* ---------- État ---------- */

  async function sonderEtat() {
    if (ferme) return;
    try {
      etat = await lire('/api/etat');
      echecsEtat = 0;
      $('#bandeau-panne').hidden = true;
      rendreEtat();
    } catch (e) {
      echecsEtat += 1;
      if (echecsEtat >= 3) $('#bandeau-panne').hidden = false;
    }
    setTimeout(sonderEtat, document.hidden ? 4000 : 1000);
  }

  function rendreEtat() {
    if (!etat) return;
    $('#version-entete').textContent = etat.version ? 'v' + etat.version : '';

    const enrole = !!etat.enrole;
    $('#bloc-enrolement').hidden = enrole;
    $('#bloc-relais').hidden = !enrole;

    rendreHorloge(etat.horloge);
    rendreMaj(etat.maj);
    rendreAssistance(etat.assistance);
    if (ongletMolette === 'm-onglet-enrolement') rendreEnrolementMolette();
    if (!enrole) { rendreCodeInconnu(); return; }

    const r = etat.relais || {};
    const phase = PHASES[r.phase] ? r.phase : (r.actif ? 'connexion' : 'arret');
    const carte = $('#carte-etat');
    carte.dataset.phase = phase;
    const libelle = PHASES[phase];
    if ($('#etat-phase').textContent !== libelle) $('#etat-phase').textContent = libelle;
    $('#etat-message').textContent = r.message || MESSAGES_PAR_DEFAUT[phase];

    const pp = r.pp;
    if (pp && (pp.port || pp.version || pp.nom)) {
      const morceaux = [pp.nom || 'ProPresenter'];
      if (pp.version) morceaux.push(pp.version);
      let t = morceaux.join(' ');
      if (pp.port) t += ', port ' + pp.port;
      if (r.transport) t += ' (' + r.transport + ')';
      $('#etat-pp').textContent = t;
    } else {
      $('#etat-pp').textContent = r.actif ? 'Pas encore trouvé' : 'Non recherché';
    }

    const c = r.cible;
    if (c && c.url) $('#etat-cible').textContent = hoteDe(c.url) + (c.connectee ? ' (connecté)' : ' (non connecté)');
    else $('#etat-cible').textContent = '-';

    // depuis = heure du dernier changement de dernier_texte ; phase_depuis (ajout du relais,
    // hors contrat) = heure d'entrée dans la phase affichée. Sans phase_depuis, on retombe sur depuis.
    const dt = versTexte(r.dernier_texte).trim();
    const dTexte = enDate(r.depuis);
    $('#etat-texte').textContent = dt ? '« ' + dt + ' »' + (dTexte ? ' à ' + heure(dTexte) : '') : 'Aucun texte envoyé';
    $('#etat-texte').title = dt;

    const d = enDate(r.phase_depuis !== undefined ? r.phase_depuis : r.depuis);
    $('#etat-depuis').textContent = d ? heure(d) + ' (' + duree(Date.now() - d.getTime()) + ')' : '-';

    const actif = !!r.actif;
    $('#bouton-demarrer').disabled = actif;
    $('#bouton-arreter').disabled = !actif;

    if (!modeEnCours) {
      const mode = etat.mode === 'manuel' ? 'manuel' : 'auto';
      $$('.bascule [role="radio"]').forEach((b) => {
        const sel = b.dataset.mode === mode;
        b.setAttribute('aria-checked', String(sel));
        b.tabIndex = sel ? 0 : -1;
      });
    }
  }

  function rendreHorloge(h) {
    const ko = h && h.ok === false;
    $('#bandeau-horloge').hidden = !ko;
    if (!ko) return;
    let t;
    if (typeof h.ecart_s === 'number') {
      const ecart = Math.abs(h.ecart_s);
      const sens = h.ecart_s > 0 ? 'en avance' : 'en retard';
      const valeur = ecart >= 120 ? Math.round(ecart / 60) + ' min' : Math.round(ecart) + ' s';
      t = 'Ce PC est ' + sens + ' de ' + valeur + ' sur le serveur : les codes à 6 chiffres seront refusés. ';
    } else {
      t = 'Impossible de comparer l\'heure de ce PC avec celle du serveur. ';
    }
    t += 'Réglez l\'heure dans Windows : Paramètres, Heure et langue, Synchroniser maintenant.';
    $('#bandeau-horloge-texte').textContent = t;
  }

  /* ---------- Démarrer, arrêter, mode ---------- */

  $('#bouton-demarrer').addEventListener('click', (e) => avecBouton(e.currentTarget, async () => {
    try { await envoyer('/api/relais/demarrer'); annoncer('Démarrage du relais.'); sonderUneFois(); }
    catch (err) { annoncer('Démarrage impossible : ' + err.message, true); }
  }));

  $('#bouton-arreter').addEventListener('click', (e) => avecBouton(e.currentTarget, async () => {
    const enLigne = etat && etat.relais && etat.relais.phase === 'en_ligne';
    if (enLigne && !(await confirmer('Arrêter le relais ?', 'Le site ne recevra plus les textes de ProPresenter tant que le relais restera arrêté.', 'Arrêter le relais'))) return;
    try { await envoyer('/api/relais/arreter'); annoncer('Relais arrêté.'); sonderUneFois(); }
    catch (err) { annoncer('Arrêt impossible : ' + err.message, true); }
  }));

  let modeEnCours = false;
  async function choisirMode(mode) {
    if (modeEnCours || (etat && etat.mode === mode)) return;
    modeEnCours = true;
    const boutons = $$('.bascule [role="radio"]');
    boutons.forEach((b) => { b.setAttribute('aria-checked', String(b.dataset.mode === mode)); b.tabIndex = b.dataset.mode === mode ? 0 : -1; });
    try {
      await envoyer('/api/mode', { mode });
      if (etat) etat.mode = mode;
      annoncer(mode === 'auto' ? 'Mode Auto : ProPresenter pilote les textes.' : 'Mode Manuel : la régie a la main.');
    } catch (err) {
      annoncer('Changement de mode impossible : ' + err.message, true);
    } finally {
      modeEnCours = false;
      rendreEtat();
    }
  }
  $$('.bascule [role="radio"]').forEach((b, i, tous) => {
    b.addEventListener('click', () => choisirMode(b.dataset.mode));
    b.addEventListener('keydown', (e) => {
      if (['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp'].includes(e.key)) {
        e.preventDefault();
        const autre = tous[(i + 1) % tous.length];
        autre.focus();
        choisirMode(autre.dataset.mode);
      }
    });
  });

  let sondageUnique = null;
  function sonderUneFois() {
    clearTimeout(sondageUnique);
    sondageUnique = setTimeout(async () => {
      try { etat = await lire('/api/etat'); rendreEtat(); } catch (_) { /* le sondage régulier s'en charge */ }
    }, 250);
  }

  /* ---------- Code à 6 chiffres ---------- */

  function rendreCodeInconnu() {
    codeAffiche = '';
    $('#code-a').textContent = '---';
    $('#code-b').textContent = '---';
    $('#code').classList.add('inconnu');
    $('#code-reste').textContent = 'Code indisponible';
    $('#code-barre').style.width = '0';
    $('#bouton-copier').disabled = true;
  }

  async function sonderCode() {
    if (ferme) return;
    if (etat && etat.enrole) {
      try {
        const c = await lire('/api/code');
        const code = String(c.code || '').replace(/\D/g, '');
        const reste = Math.max(0, Math.min(30, Number(c.reste_s) || 0));
        if (code.length === 6) {
          codeAffiche = code;
          $('#code-a').textContent = code.slice(0, 3);
          $('#code-b').textContent = code.slice(3);
          $('#code').classList.remove('inconnu');
          $('#code').setAttribute('aria-label', 'Code ' + code.split('').join(' '));
          $('#bouton-copier').disabled = false;
        } else {
          rendreCodeInconnu();
        }
        const barre = $('#code-barre');
        // Remontée de la barre (nouveau code) sans animation, puis descente fluide.
        if (codeReste !== null && reste > codeReste) {
          barre.classList.add('sans-transition');
          barre.style.width = (reste / 30 * 100) + '%';
          void barre.offsetWidth;
          barre.classList.remove('sans-transition');
        } else {
          barre.style.width = (reste / 30 * 100) + '%';
        }
        barre.classList.toggle('fin', reste <= 5);
        codeReste = reste;
        $('#code-reste').textContent = 'Nouveau code dans ' + reste + ' s';
      } catch (e) {
        rendreCodeInconnu();
        if (e.statut === 403) sonderUneFois();
      }
    }
    setTimeout(sonderCode, document.hidden ? 3000 : 1000);
  }

  async function copier(texte) {
    try {
      await navigator.clipboard.writeText(texte);
      return true;
    } catch (_) {
      const zone = document.createElement('textarea');
      zone.value = texte;
      zone.setAttribute('readonly', '');
      zone.style.position = 'fixed';
      zone.style.opacity = '0';
      document.body.appendChild(zone);
      zone.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (__) { ok = false; }
      zone.remove();
      return ok;
    }
  }

  $('#bouton-copier').addEventListener('click', async () => {
    if (!codeAffiche) return;
    const b = $('#bouton-copier');
    if (await copier(codeAffiche)) {
      b.textContent = 'Copié';
      annoncer('Code copié. Collez-le sur tech.ame.paris.');
      setTimeout(() => { b.textContent = 'Copier'; }, 2000);
    } else {
      annoncer('Copie impossible, recopiez le code à la main.', true);
    }
  });

  /* ---------- Enrôlement ---------- */

  $('#form-enrolement').addEventListener('submit', async (e) => {
    e.preventDefault();
    const champ = $('#texte-enrolement');
    const texte = champ.value.trim();
    const erreur = $('#erreur-enrolement');
    erreur.hidden = true;
    if (!/^(AMETECH1:|otpauth:\/\/)/i.test(texte)) {
      erreur.textContent = 'Ce texte ne ressemble pas à un code d\'enrôlement : il doit commencer par AMETECH1: ou otpauth://.';
      erreur.hidden = false;
      champ.focus();
      return;
    }
    await avecBouton($('#bouton-enroler'), async () => {
      try {
        await envoyer('/api/enroler', { texte });
        champ.value = '';
        annoncer('Poste enrôlé. Vous pouvez démarrer le relais.');
        etat = await lire('/api/etat');
        rendreEtat();
        const d = $('#bouton-demarrer');
        if (d && !d.disabled) d.focus();
      } catch (err) {
        erreur.textContent = 'Enrôlement refusé : ' + err.message;
        erreur.hidden = false;
        champ.focus();
      }
    });
  });

  function rendreEnrolementMolette() {
    const enrole = !!(etat && etat.enrole);
    $('#enrolement-etat').textContent = enrole ? 'Ce poste est enrôlé : il affiche le code d\'accès et peut ouvrir la session du relais.' : 'Ce poste n\'est pas enrôlé.';
    $('#bouton-oublier').disabled = !enrole;
  }

  $('#bouton-oublier').addEventListener('click', (e) => avecBouton(e.currentTarget, async () => {
    if (!(await confirmer('Oublier l\'enrôlement ?', 'Le secret de ce PC sera effacé et le relais s\'arrêtera. Il faudra un nouveau code d\'enrôlement de Will Hardy.', 'Oublier l\'enrôlement'))) return;
    try {
      await envoyer('/api/oublier');
      annoncer('Enrôlement oublié.');
      etat = await lire('/api/etat');
      rendreEtat();
      $('#molette').close();
    } catch (err) { annoncer('Impossible d\'oublier l\'enrôlement : ' + err.message, true); }
  }));

  /* ---------- Mises à jour ---------- */

  function rendreMaj(m) {
    m = m || {};
    const statut = STATUTS_MAJ[m.statut] ? m.statut : 'inconnu';
    const verifie = enDate(m.verifie_le);
    const html = (carte) => {
      if (!carte.firstChild) {
        carte.innerHTML =
          '<div class="maj-ligne"><h2>Mises à jour</h2><span class="statut-maj"></span></div>' +
          '<p class="maj-texte"></p><p class="aide maj-aide"></p>' +
          '<div class="rangee"><button type="button" class="bouton bouton-secondaire bouton-petit" data-maj-verifier>Vérifier</button>' +
          '<button type="button" class="bouton bouton-or bouton-petit" data-maj-installer hidden>Installer</button></div>';
        carte.querySelector('[data-maj-verifier]').addEventListener('click', verifierMaj);
        carte.querySelector('[data-maj-installer]').addEventListener('click', installerMaj);
      }
      const pastille = carte.querySelector('.statut-maj');
      pastille.dataset.statut = statut;
      pastille.textContent = STATUTS_MAJ[statut];
      let texte = 'Version installée : ' + (m.version_locale || (etat && etat.version) || 'inconnue');
      if (m.version_dispo && (statut === 'disponible' || statut === 'telechargement' || statut === 'pret')) texte += ', nouvelle version : ' + m.version_dispo;
      carte.querySelector('.maj-texte').textContent = texte;
      const aide = [];
      if (m.message) aide.push(m.message);
      if (verifie) aide.push('Vérifié à ' + heure(verifie) + (new Date().toDateString() === verifie.toDateString() ? '' : ' le ' + verifie.toLocaleDateString('fr-FR')));
      carte.querySelector('.maj-aide').textContent = aide.join('. ');
      const verif = carte.querySelector('[data-maj-verifier]');
      const inst = carte.querySelector('[data-maj-installer]');
      verif.disabled = majEnCours || statut === 'telechargement';
      inst.hidden = !(statut === 'disponible' || statut === 'pret');
      inst.disabled = majEnCours;
    };
    $$('[data-maj]').forEach(html);
  }

  async function verifierMaj() {
    if (majEnCours) return;
    majEnCours = true;
    rendreMaj(etat && etat.maj);
    try {
      const m = await envoyer('/api/maj/verifier');
      if (etat && m) etat.maj = m;
      annoncer(m && m.statut === 'disponible' ? 'Une mise à jour est disponible.' : (m && m.statut === 'erreur' ? 'Vérification en échec.' : 'AME Tech est à jour.'), m && m.statut === 'erreur');
    } catch (err) { annoncer('Vérification impossible : ' + err.message, true); }
    finally { majEnCours = false; rendreMaj(etat && etat.maj); }
  }

  async function installerMaj() {
    if (majEnCours) return;
    const enLigne = etat && etat.relais && etat.relais.phase === 'en_ligne';
    const texte = enLigne
      ? 'Le relais est en ligne : il sera coupé pendant le redémarrage d\'AME Tech (moins d\'une minute). Évitez de le faire pendant le culte.'
      : 'AME Tech va télécharger la nouvelle version, vérifier sa signature puis redémarrer.';
    if (!(await confirmer('Installer la mise à jour ?', texte, 'Installer', false))) return;
    majEnCours = true;
    rendreMaj(etat && etat.maj);
    try {
      const m = await envoyer('/api/maj/appliquer');
      if (etat && m) etat.maj = m;
      annoncer(m && m.statut === 'erreur' ? 'Installation en échec : ' + (m.message || 'voir le journal') : 'Installation lancée. La fenêtre va se recharger.', m && m.statut === 'erreur');
    } catch (err) { annoncer('Installation impossible : ' + err.message, true); }
    finally { majEnCours = false; rendreMaj(etat && etat.maj); }
  }

  /* ---------- Assistance ---------- */

  function formaterIdentifiant(id) {
    const s = String(id).replace(/\s+/g, '');
    return /^\d+$/.test(s) ? s.replace(/(\d{3})(?=\d)/g, '$1 ') : s;
  }

  function rendreAssistance(a, force) {
    a = a || {};
    const bloc = $('#rustdesk-id-bloc');
    const etatTexte = $('#assistance-etat');
    const bouton = $('#bouton-rustdesk');
    if (a.id && (a.rustdesk === 'lance' || force)) {
      bloc.hidden = false;
      const f = formaterIdentifiant(a.id);
      if ($('#rustdesk-id').textContent !== f) $('#rustdesk-id').textContent = f;
      if (!rustdeskEnCours) etatTexte.textContent = 'RustDesk est ouvert. Dictez cet identifiant à Will Hardy, puis acceptez sa connexion.';
      bouton.textContent = 'Relancer l\'assistance';
    } else {
      bloc.hidden = true;
      if (!rustdeskEnCours) etatTexte.textContent = assistanceMessage;
      bouton.textContent = 'Assistance à distance';
    }
  }

  $('#bouton-rustdesk').addEventListener('click', (e) => avecBouton(e.currentTarget, async () => {
    rustdeskEnCours = true;
    const premier = !etat || !etat.assistance || etat.assistance.rustdesk === 'absent';
    $('#assistance-etat').textContent = premier
      ? 'Préparation de l\'assistance : téléchargement de RustDesk (25 Mo), cela peut prendre 30 secondes...'
      : 'Ouverture de RustDesk...';
    try {
      const a = await envoyer('/api/assistance/rustdesk');
      rustdeskEnCours = false;
      if (a && a.id && a.ok !== false) {
        assistanceMessage = '';
        if (etat) etat.assistance = Object.assign({}, etat.assistance, a);
        rendreAssistance(a, true);
        annoncer('Identifiant RustDesk : ' + formaterIdentifiant(a.id));
      } else {
        // ok=false (hors Windows, appel déjà en cours, téléchargement refusé) : message du module tel quel.
        assistanceMessage = (a && (a.message || a.erreur)) || 'RustDesk est lancé, l\'identifiant apparaîtra dans quelques secondes.';
        $('#assistance-etat').textContent = assistanceMessage;
        if (a && a.ok === false) annoncer(a.message || 'Assistance à distance indisponible.', true);
      }
    } catch (err) {
      rustdeskEnCours = false;
      assistanceMessage = 'Assistance à distance indisponible : ' + err.message + '. Essayez TeamViewer QuickSupport ou appelez Will Hardy.';
      $('#assistance-etat').textContent = assistanceMessage;
    }
  }));

  /* ---------- Quitter ---------- */

  $('#bouton-quitter').addEventListener('click', async () => {
    const enLigne = etat && etat.relais && etat.relais.phase === 'en_ligne';
    const texte = (enLigne ? 'Le relais est en ligne : ' : '') + 'AME Tech va s\'arrêter et le site ne recevra plus les textes de ProPresenter.';
    if (!(await confirmer('Quitter AME Tech ?', texte, 'Quitter'))) return;
    try {
      await envoyer('/api/quitter');
    } catch (err) {
      annoncer('Arrêt impossible : ' + err.message, true);
      return;
    }
    ferme = true;
    arreterFlux();
    $('#ecran-ferme').hidden = false;
    setTimeout(() => { try { window.close(); } catch (_) { /* fenêtre non fermable par script */ } }, 800);
  });

  /* ---------- Débug : journal ---------- */

  const NIVEAUX = { info: 'Info', alerte: 'Alerte', erreur: 'Erreur' };

  function ligneJournal(l) {
    const div = document.createElement('div');
    div.className = 'ligne';
    const niveau = NIVEAUX[l.niveau] ? l.niveau : 'info';
    div.dataset.niveau = niveau;
    const d = enDate(l.t);
    const cellules = [
      ['heure', d ? heure(d, true) : ''],
      ['niveau', NIVEAUX[niveau]],
      ['etape', l.etape || ''],
      ['message', versTexte(l.message)],
    ];
    for (const [cls, txt] of cellules) {
      const s = document.createElement('span');
      s.className = cls;
      s.textContent = txt;
      div.appendChild(s);
    }
    return div;
  }

  function rendreJournal() {
    const zone = $('#journal');
    const filtre = $('#filtre-niveau').value;
    const visibles = journalLignes.filter((l) => !filtre || l.niveau === filtre);
    zone.textContent = '';
    if (!visibles.length) {
      const p = document.createElement('p');
      p.className = 'vide';
      p.textContent = journalLignes.length ? 'Aucune ligne pour ce niveau.' : 'Journal vide pour le moment.';
      zone.appendChild(p);
      return;
    }
    const frag = document.createDocumentFragment();
    visibles.forEach((l) => frag.appendChild(ligneJournal(l)));
    zone.appendChild(frag);
    defilerJournal();
  }

  function ajouterJournal(nouvelles) {
    const zone = $('#journal');
    const filtre = $('#filtre-niveau').value;
    journalLignes = journalLignes.concat(nouvelles);
    if (journalLignes.length > 2000) {
      journalLignes = journalLignes.slice(-2000);
      rendreJournal();
      return;
    }
    const visibles = nouvelles.filter((l) => !filtre || l.niveau === filtre);
    if (!visibles.length) { if (!zone.firstChild) rendreJournal(); return; }
    const vide = zone.querySelector('.vide');
    if (vide) vide.remove();
    const frag = document.createDocumentFragment();
    visibles.forEach((l) => frag.appendChild(ligneJournal(l)));
    zone.appendChild(frag);
    defilerJournal();
  }

  function defilerJournal(force) {
    if (!force && !$('#suivre-journal').checked) return;
    const zone = $('#journal');
    zone.scrollTop = zone.scrollHeight;
  }

  function majPastille() {
    const p = $('#pastille-erreurs');
    p.hidden = erreursNonLues === 0;
    p.textContent = erreursNonLues > 99 ? '99+' : String(erreursNonLues);
    p.setAttribute('aria-label', erreursNonLues + ' nouvelles erreurs');
  }

  async function sonderJournal() {
    if (ferme) return;
    try {
      const j = await lire('/api/journal?depuis=' + encodeURIComponent(journalDernier));
      const lignes = Array.isArray(j.lignes) ? j.lignes : [];
      if (typeof j.dernier === 'number') {
        // Journal remis à zéro côté serveur (redémarrage) : on repart du début.
        if (j.dernier < journalDernier) { journalLignes = []; rendreJournal(); }
        journalDernier = j.dernier;
      }
      if (lignes.length) {
        ajouterJournal(lignes);
        if (!premierJournal && ongletPrincipal !== 'onglet-debug') {
          erreursNonLues += lignes.filter((l) => l.niveau === 'erreur').length;
          majPastille();
        }
      } else if (premierJournal) {
        rendreJournal();
      }
      premierJournal = false;
    } catch (_) { /* le bandeau de panne vient de /api/etat */ }
    const visible = ongletPrincipal === 'onglet-debug' && !document.hidden;
    setTimeout(sonderJournal, visible ? 1000 : 3000);
  }

  $('#filtre-niveau').addEventListener('change', rendreJournal);
  $('#suivre-journal').addEventListener('change', () => defilerJournal());
  $('#bouton-vider-journal').addEventListener('click', () => { journalLignes = []; rendreJournal(); });

  /* ---------- Débug : diagnostic ---------- */

  let diagnosticEnCours = false;
  async function chargerDiagnostic() {
    if (diagnosticEnCours) return;
    diagnosticEnCours = true;
    const liste = $('#liste-diagnostic');
    const bouton = $('#bouton-diagnostic');
    bouton.disabled = true;
    liste.setAttribute('aria-busy', 'true');
    if (!liste.firstChild) liste.innerHTML = '<li><span class="pastille-diag">…</span><span class="nom-diag">Test en cours</span><span class="detail-diag">Chaque maillon est vérifié.</span></li>';
    try {
      const d = await lire('/api/diagnostic');
      liste.textContent = '';
      (Array.isArray(d.etapes) ? d.etapes : []).forEach((et) => {
        const li = document.createElement('li');
        const ok = et.ok === true ? 'true' : et.ok === false ? 'false' : 'inconnu';
        li.dataset.ok = ok;
        const p = document.createElement('span');
        p.className = 'pastille-diag';
        p.setAttribute('aria-hidden', 'true');
        p.textContent = ok === 'true' ? '✓' : ok === 'false' ? '✕' : '?';
        const n = document.createElement('span');
        n.className = 'nom-diag';
        n.textContent = (et.nom || 'Étape') + (ok === 'true' ? ' : OK' : ok === 'false' ? ' : en échec' : ' : non testé');
        const det = document.createElement('span');
        det.className = 'detail-diag';
        det.textContent = versTexte(et.detail);
        li.append(p, n, det);
        liste.appendChild(li);
      });
      if (!liste.firstChild) liste.innerHTML = '<li><span class="pastille-diag">?</span><span class="nom-diag">Aucune étape renvoyée</span><span class="detail-diag"></span></li>';
    } catch (err) {
      liste.innerHTML = '';
      const li = document.createElement('li');
      li.dataset.ok = 'false';
      li.innerHTML = '<span class="pastille-diag" aria-hidden="true">✕</span><span class="nom-diag">Diagnostic impossible</span><span class="detail-diag"></span>';
      li.querySelector('.detail-diag').textContent = err.message;
      liste.appendChild(li);
    } finally {
      diagnosticEnCours = false;
      bouton.disabled = false;
      liste.removeAttribute('aria-busy');
    }
  }
  $('#bouton-diagnostic').addEventListener('click', chargerDiagnostic);

  /* ---------- Molette technique ---------- */

  const molette = $('#molette');
  $('#bouton-molette').addEventListener('click', () => {
    molette.showModal();
    if (ongletMolette === 'm-onglet-pp') chargerPpInfo();
    if (ongletMolette === 'm-onglet-reglages') chargerReglages();
    if (ongletMolette === 'm-onglet-enrolement') rendreEnrolementMolette();
  });
  molette.querySelector('[data-fermer]').addEventListener('click', () => molette.close());
  molette.addEventListener('close', () => { arreterFlux(); reglagesCharges = false; $('#bouton-molette').focus(); });
  molette.addEventListener('click', (e) => { if (e.target === molette) molette.close(); });

  async function chargerPpInfo() {
    const dl = $('#pp-info');
    dl.textContent = '';
    try {
      const info = await lire('/api/pp/info');
      const entrees = info && typeof info === 'object' ? Object.entries(info) : [['Réponse', info]];
      if (!entrees.length) entrees.push(['Réponse', 'vide']);
      for (const [k, v] of entrees) {
        const dt = document.createElement('dt');
        dt.textContent = k;
        const dd = document.createElement('dd');
        dd.textContent = v === null || v === undefined ? '-' : versTexte(v);
        dl.append(dt, dd);
      }
    } catch (err) {
      const dt = document.createElement('dt');
      dt.textContent = 'Erreur';
      const dd = document.createElement('dd');
      dd.textContent = err.message;
      dl.append(dt, dd);
    }
  }
  $('#bouton-pp-info').addEventListener('click', chargerPpInfo);

  $('#form-requete').addEventListener('submit', async (e) => {
    e.preventDefault();
    const methode = $('#req-methode').value;
    const chemin = $('#req-chemin').value.trim();
    const statut = $('#req-statut');
    const sortie = $('#req-reponse');
    if (!chemin.startsWith('/v1/')) {
      statut.textContent = 'Le chemin doit commencer par /v1/.';
      $('#req-chemin').focus();
      return;
    }
    const corpsBrut = $('#req-corps').value.trim();
    const demande = { methode, chemin };
    if (methode !== 'GET' && corpsBrut) {
      try { demande.corps = JSON.parse(corpsBrut); }
      catch (_) { statut.textContent = 'Corps JSON invalide.'; $('#req-corps').focus(); return; }
    } else if (methode !== 'GET') {
      demande.corps = null;
    }
    const bouton = e.submitter || $('#form-requete [type="submit"]');
    await avecBouton(bouton, async () => {
      statut.textContent = 'Envoi de ' + methode + ' ' + chemin + '...';
      const t0 = performance.now();
      try {
        const r = await envoyer('/api/pp/requete', demande);
        const ms = Math.round(performance.now() - t0);
        statut.textContent = 'Statut ' + (r && r.statut !== undefined ? r.statut : '?') + ' en ' + ms + ' ms';
        let corps = r ? r.corps : null;
        if (typeof corps === 'string') { try { corps = JSON.parse(corps); } catch (_) { /* texte brut */ } }
        sortie.textContent = corps === undefined || corps === null || corps === '' ? '(réponse vide)' : versTexte(corps);
      } catch (err) {
        statut.textContent = 'Échec : ' + err.message;
        sortie.textContent = '';
      }
    });
  });
  $('#req-methode').addEventListener('change', () => { $('#req-corps').disabled = $('#req-methode').value === 'GET'; });
  $('#req-corps').disabled = true;

  function ajouterEvenement(texte, nom) {
    const zone = $('#flux');
    const div = document.createElement('div');
    div.className = 'evenement';
    const t = document.createElement('time');
    t.textContent = heure(new Date(), true) + (nom && nom !== 'message' ? ' ' + nom : '');
    let contenu = texte;
    try { contenu = JSON.stringify(JSON.parse(texte)); } catch (_) { /* brut */ }
    div.append(t, document.createTextNode(contenu));
    zone.appendChild(div);
    while (zone.childElementCount > 300) zone.firstElementChild.remove();
    zone.scrollTop = zone.scrollHeight;
  }

  function demarrerFlux() {
    arreterFlux();
    const src = new EventSource('/api/pp/flux');
    fluxSource = src;
    const b = $('#bouton-flux');
    b.textContent = 'Arrêter';
    b.setAttribute('aria-pressed', 'true');
    $('#flux-etat').textContent = 'Connexion au flux...';
    let n = 0;
    const recevoir = (ev) => { n += 1; $('#flux-etat').textContent = 'En écoute, ' + n + ' événement' + (n > 1 ? 's' : '') + ' reçu' + (n > 1 ? 's' : '') + '.'; ajouterEvenement(ev.data, ev.type); };
    src.onopen = () => { if (!n) $('#flux-etat').textContent = 'En écoute, en attente d\'événements.'; };
    src.onmessage = recevoir;
    ['etat', 'erreur'].forEach((type) => src.addEventListener(type, recevoir));
    src.onerror = () => { if (fluxSource === src) $('#flux-etat').textContent = 'Flux interrompu, nouvelle tentative automatique...'; };
  }

  function arreterFlux() {
    if (fluxSource) { fluxSource.close(); fluxSource = null; }
    const b = $('#bouton-flux');
    b.textContent = 'Écouter';
    b.setAttribute('aria-pressed', 'false');
    $('#flux-etat').textContent = 'Flux arrêté.';
  }

  $('#bouton-flux').addEventListener('click', () => (fluxSource ? arreterFlux() : demarrerFlux()));
  $('#bouton-flux-vider').addEventListener('click', () => { $('#flux').textContent = ''; });

  /* Réglages */
  async function chargerReglages(force) {
    if (reglagesCharges && !force) return;
    const msg = $('#reg-message');
    try {
      const r = await lire('/api/reglages');
      $('#reg-cibles').value = (Array.isArray(r.cibles) ? r.cibles : []).join('\n');
      $('#reg-port').value = Number.isFinite(Number(r.port_pp)) ? String(Number(r.port_pp)) : '0';
      $('#reg-demarrage').checked = !!r.demarrage_windows;
      $('#reg-lancement').checked = !!r.relais_au_lancement;
      reglagesCharges = true;
      msg.textContent = '';
    } catch (err) {
      msg.textContent = 'Lecture des réglages impossible : ' + err.message;
    }
  }
  $('#bouton-reglages-recharger').addEventListener('click', () => chargerReglages(true));

  $('#form-reglages').addEventListener('submit', async (e) => {
    e.preventDefault();
    const msg = $('#reg-message');
    const cibles = $('#reg-cibles').value.split(/\s*\n\s*/).map((s) => s.trim()).filter(Boolean);
    const mauvaises = cibles.filter((c) => !/^https?:\/\/[^\s/]+/i.test(c));
    if (!cibles.length) { msg.textContent = 'Indiquez au moins un serveur cible.'; $('#reg-cibles').focus(); return; }
    if (mauvaises.length) { msg.textContent = 'Adresse invalide : ' + mauvaises[0] + ' (elle doit commencer par https://).'; $('#reg-cibles').focus(); return; }
    const port = Number($('#reg-port').value || 0);
    if (!Number.isInteger(port) || port < 0 || port > 65535) { msg.textContent = 'Port invalide : entre 0 et 65535.'; $('#reg-port').focus(); return; }
    const corps = { cibles, port_pp: port, demarrage_windows: $('#reg-demarrage').checked, relais_au_lancement: $('#reg-lancement').checked };
    const bouton = e.submitter || $('#form-reglages [type="submit"]');
    await avecBouton(bouton, async () => {
      try {
        await envoyer('/api/reglages', corps);
        msg.textContent = 'Réglages enregistrés.';
        await chargerReglages(true);
        msg.textContent = 'Réglages enregistrés.';
      } catch (err) {
        msg.textContent = 'Enregistrement refusé : ' + err.message;
      }
    });
  });

  /* ---------- Lancement ---------- */

  document.addEventListener('visibilitychange', () => { if (!document.hidden) sonderUneFois(); });
  sonderEtat();
  sonderCode();
  sonderJournal();
})();
