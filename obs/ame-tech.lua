--[[
AME Tech pour OBS (Mac et Windows, OBS 30 et plus).

  - crée la scène « Habillage » et sa source navigateur 1920x1080 ;
  - « Rafraîchir l'habillage » (bouton et raccourci) vide le cache de toutes les sources
    navigateur dont l'adresse commence par celle du site ;
  - rafraîchit tout seul quand le site publie une nouvelle version (GET /api/version, toutes
    les 30 s), seulement si rien n'est à l'antenne (GET /api/etat?view=<jeton>) ;
  - affiche l'adresse du panneau à ajouter en dock.

Le jeton de lecture vient du lien « Habillage » collé dans les réglages du script : il reste dans
les réglages d'OBS de ce poste et n'est jamais écrit dans ce fichier ni dans le journal.

Requêtes HTTP : libcurl, déjà présente dans OBS (Windows : libcurl.dll livrée avec OBS ; Mac :
/usr/lib/libcurl.4.dylib du système), appelée par FFI LuaJIT en mode « multi », c'est-à-dire
sans jamais attendre le réseau. Les minuteurs des scripts tournent dans le fil de rendu d'OBS :
un appel bloquant (os.execute, io.popen) y figerait l'image, et sous Windows chaque appel
ouvrirait une fenêtre de console. Ici, pas de processus externe, pas de fichier temporaire,
le jeton ne passe pas en ligne de commande, et le certificat HTTPS est vérifié par libcurl.
Sans libcurl, seul le rafraîchissement automatique est désactivé ; le bouton et le raccourci
continuent de marcher.
]]

obs = obslua

local VERSION_SCRIPT = "1.0.1"
local SITE_DEFAUT = "https://tech.ame.paris"
local NOM_SCENE = "Habillage"
local NOM_SOURCE = "Habillage AME"
local SONDAGE_S = 30
local DELAI_MAX_S = 20
local TAILLE_MAX = 512 * 1024
local CLES_ANTENNE = { "lt", "verset", "chant", "carton", "bandeau", "don", "donner", "whatsapp", "media", "plein" }

local site = SITE_DEFAUT
local lien = ""
local jeton = nil
local probleme_lien = nil
local probleme_site = nil
local mes_reglages = nil
local raccourci = nil
local etat_texte = "Démarrage…"
local etat_alerte = false
local version_vue = nil
local version_attendue = nil
local prochain_sondage = 0
local scenes_a_liberer = {}

------------------------------------------------------------------ journal et état
local function journal(niveau, message)
  obs.script_log(niveau, message)
end

local function definir_etat(texte, alerte)
  etat_texte, etat_alerte = texte, alerte and true or false
  if mes_reglages ~= nil then obs.obs_data_set_string(mes_reglages, "etat_script", texte) end
end

local function heure()
  return os.date("%H:%M:%S")
end

------------------------------------------------------------------ JSON (lecture seule, compact)
local json = {}
do
  local function erreur(i, quoi) error({ position = i, message = quoi }) end
  local function espaces(s, i)
    local _, fin = s:find("^[ \t\r\n]*", i)
    return fin + 1
  end
  local function utf8(c)
    if c < 0x80 then return string.char(c) end
    if c < 0x800 then return string.char(0xC0 + math.floor(c / 0x40), 0x80 + c % 0x40) end
    if c < 0x10000 then
      return string.char(0xE0 + math.floor(c / 0x1000), 0x80 + math.floor(c / 0x40) % 0x40, 0x80 + c % 0x40)
    end
    return string.char(0xF0 + math.floor(c / 0x40000), 0x80 + math.floor(c / 0x1000) % 0x40,
      0x80 + math.floor(c / 0x40) % 0x40, 0x80 + c % 0x40)
  end
  local valeur
  local function chaine(s, i)
    local morceaux, j = {}, i + 1
    while true do
      local debut, fin, avant, special = s:find('^([^"\\]*)(["\\])', j)
      if not debut then erreur(i, "chaîne non terminée") end
      morceaux[#morceaux + 1] = avant
      j = fin + 1
      if special == '"' then return table.concat(morceaux), j end
      local e = s:sub(j, j)
      local simples = { ['"'] = '"', ["\\"] = "\\", ["/"] = "/", b = "\b", f = "\f", n = "\n", r = "\r", t = "\t" }
      if simples[e] then
        morceaux[#morceaux + 1] = simples[e]
        j = j + 1
      elseif e == "u" then
        local hex = s:sub(j + 1, j + 4)
        if not hex:find("^%x%x%x%x$") then erreur(j, "échappement \\u invalide") end
        local c = tonumber(hex, 16)
        j = j + 5
        if c >= 0xD800 and c <= 0xDBFF and s:sub(j, j + 1) == "\\u" then
          local bas = tonumber(s:sub(j + 2, j + 5), 16)
          if bas and bas >= 0xDC00 and bas <= 0xDFFF then
            c = 0x10000 + (c - 0xD800) * 0x400 + (bas - 0xDC00)
            j = j + 6
          end
        end
        morceaux[#morceaux + 1] = utf8(c)
      else
        erreur(j, "échappement invalide")
      end
    end
  end
  function valeur(s, i, profondeur)
    if profondeur > 64 then erreur(i, "imbrication trop profonde") end
    i = espaces(s, i)
    local c = s:sub(i, i)
    if c == "{" then
      local objet = {}
      i = espaces(s, i + 1)
      if s:sub(i, i) == "}" then return objet, i + 1 end
      while true do
        if s:sub(i, i) ~= '"' then erreur(i, "clé attendue") end
        local cle
        cle, i = chaine(s, i)
        i = espaces(s, i)
        if s:sub(i, i) ~= ":" then erreur(i, "deux-points attendus") end
        objet[cle], i = valeur(s, i + 1, profondeur + 1)
        i = espaces(s, i)
        local sep = s:sub(i, i)
        if sep == "}" then return objet, i + 1 end
        if sep ~= "," then erreur(i, "virgule attendue") end
        i = espaces(s, i + 1)
      end
    elseif c == "[" then
      local liste, n = {}, 0
      i = espaces(s, i + 1)
      if s:sub(i, i) == "]" then return liste, i + 1 end
      while true do
        n = n + 1
        liste[n], i = valeur(s, i, profondeur + 1)
        i = espaces(s, i)
        local sep = s:sub(i, i)
        if sep == "]" then return liste, i + 1 end
        if sep ~= "," then erreur(i, "virgule attendue") end
        i = i + 1
      end
    elseif c == '"' then
      return chaine(s, i)
    elseif s:sub(i, i + 3) == "true" then
      return true, i + 4
    elseif s:sub(i, i + 4) == "false" then
      return false, i + 5
    elseif s:sub(i, i + 3) == "null" then
      return json.null, i + 4
    else
      local nombre = s:match("^-?%d+%.?%d*[eE]?[-+]?%d*", i)
      if not nombre or nombre == "" or not tonumber(nombre) then erreur(i, "valeur inattendue") end
      return tonumber(nombre), i + #nombre
    end
  end
  json.null = setmetatable({}, { __tostring = function() return "null" end })
  function json.lire(texte)
    if type(texte) ~= "string" then return nil, "texte absent" end
    local ok, resultat, fin = pcall(valeur, texte, 1, 0)
    if not ok then
      return nil, type(resultat) == "table" and resultat.message or tostring(resultat)
    end
    if espaces(texte, fin) <= #texte then return nil, "contenu après la valeur" end
    return resultat
  end
end

------------------------------------------------------------------ HTTP non bloquant (libcurl par FFI)
local http = { dispo = false, raison = "FFI absent", en_cours = nil }
do
  local ok_ffi, ffi = pcall(require, "ffi")
  if ok_ffi then
    -- Une fonction Lua rappelée depuis du C ne doit pas tourner sous JIT : on le coupe pour ce script.
    if jit and jit.off then jit.off() end
    local ok_cdef = pcall(ffi.cdef, [[
      typedef void AMETECH_CURL;
      typedef void AMETECH_CURLM;
      typedef struct { int msg; void *easy_handle; union { void *whatever; int result; } data; } AMETECH_CURLMsg;
      int curl_global_init(long flags);
      AMETECH_CURL *curl_easy_init(void);
      int curl_easy_setopt(AMETECH_CURL *h, int option, ...);
      int curl_easy_getinfo(AMETECH_CURL *h, int info, ...);
      void curl_easy_cleanup(AMETECH_CURL *h);
      const char *curl_easy_strerror(int code);
      AMETECH_CURLM *curl_multi_init(void);
      int curl_multi_add_handle(AMETECH_CURLM *m, AMETECH_CURL *h);
      int curl_multi_remove_handle(AMETECH_CURLM *m, AMETECH_CURL *h);
      int curl_multi_perform(AMETECH_CURLM *m, int *running);
      AMETECH_CURLMsg *curl_multi_info_read(AMETECH_CURLM *m, int *msgs_in_queue);
      int curl_multi_cleanup(AMETECH_CURLM *m);
    ]])
    local candidats
    if ffi.os == "Windows" then
      candidats = { "libcurl", "libcurl-x64", "libcurl-4", "curl" }
    elseif ffi.os == "OSX" then
      candidats = { "/usr/lib/libcurl.4.dylib", "libcurl.4.dylib", "libcurl.dylib" }
    else
      candidats = { "libcurl.so.4", "libcurl.so", "curl" }
    end
    local C
    if ok_cdef then
      for _, nom in ipairs(candidats) do
        local ok, bib = pcall(ffi.load, nom)
        if ok and bib then
          local ok_sym = pcall(function() return bib.curl_multi_perform end)
          if ok_sym then C = bib break end
        end
      end
    end
    -- Valeurs de curl.h (stables depuis curl 7.x).
    local OPT_URL, OPT_WRITEFUNCTION, OPT_TIMEOUT, OPT_CONNECTTIMEOUT = 10002, 20011, 13, 78
    local OPT_NOSIGNAL, OPT_USERAGENT, OPT_FOLLOWLOCATION = 99, 10018, 52
    local INFO_RESPONSE_CODE, MSG_DONE = 0x200002, 1
    if C then
      local multi = C.curl_global_init(3) == 0 and C.curl_multi_init() or nil
      if multi ~= nil then
        local rappel = ffi.cast("size_t (*)(char *, size_t, size_t, void *)", function(ptr, taille, nombre, _)
          local total = tonumber(taille) * tonumber(nombre)
          local t = http.en_cours
          if not t then return 0 end
          t.taille = t.taille + total
          if t.taille > TAILLE_MAX then return 0 end
          t.morceaux[#t.morceaux + 1] = ffi.string(ptr, total)
          return total
        end)
        local function liberer(t)
          C.curl_multi_remove_handle(multi, t.poignee)
          C.curl_easy_cleanup(t.poignee)
        end
        function http.demarrer(url, fin)
          if http.en_cours then return false end
          local p = C.curl_easy_init()
          if p == nil then return false end
          C.curl_easy_setopt(p, OPT_URL, url)
          C.curl_easy_setopt(p, OPT_WRITEFUNCTION, rappel)
          C.curl_easy_setopt(p, OPT_NOSIGNAL, ffi.cast("long", 1))
          C.curl_easy_setopt(p, OPT_TIMEOUT, ffi.cast("long", 15))
          C.curl_easy_setopt(p, OPT_CONNECTTIMEOUT, ffi.cast("long", 8))
          C.curl_easy_setopt(p, OPT_FOLLOWLOCATION, ffi.cast("long", 0))
          C.curl_easy_setopt(p, OPT_USERAGENT, "AME-Tech-OBS/" .. VERSION_SCRIPT)
          http.en_cours = { poignee = p, url = url, fin = fin, morceaux = {}, taille = 0, debut = os.time() }
          C.curl_multi_add_handle(multi, p)
          return true
        end
        function http.pomper()
          local t = http.en_cours
          if not t then return end
          local actifs, reste = ffi.new("int[1]"), ffi.new("int[1]")
          C.curl_multi_perform(multi, actifs)
          while true do
            local m = C.curl_multi_info_read(multi, reste)
            if m == nil then break end
            if m.msg == MSG_DONE and t == http.en_cours then
              local resultat = m.data.result
              local code = ffi.new("long[1]")
              C.curl_easy_getinfo(t.poignee, INFO_RESPONSE_CODE, code)
              local erreur = resultat ~= 0 and ffi.string(C.curl_easy_strerror(resultat)) or nil
              liberer(t)
              http.en_cours = nil
              t.fin(tonumber(code[0]), table.concat(t.morceaux), erreur)
              return
            end
          end
          if os.time() - t.debut > DELAI_MAX_S then
            liberer(t)
            http.en_cours = nil
            t.fin(0, "", "délai dépassé")
          end
        end
        function http.fermer()
          if http.en_cours then liberer(http.en_cours) http.en_cours = nil end
          C.curl_multi_cleanup(multi)
          multi = nil
          rappel:free()
          http.dispo = false
        end
        http.dispo, http.raison = true, nil
      else
        http.raison = "libcurl n'a pas démarré"
      end
    else
      http.raison = ok_cdef and "libcurl introuvable" or "déclarations FFI refusées"
    end
  end
end

------------------------------------------------------------------ adresses
local function nettoyer_site(valeur)
  local s = (valeur or ""):gsub("^%s+", ""):gsub("%s+$", "")
  s = s:gsub("/+$", "")
  if s == "" then return SITE_DEFAUT end
  if not s:find("^https?://") then s = "https://" .. s end
  return s
end

-- http:// seulement sur ce poste : ailleurs, le jeton de lecture partirait en clair sur le réseau.
local function verifier_site(s)
  if not s:find("^http://") then return nil end
  local hote = s:match("^http://([^/?#]*)") or ""
  hote = hote:gsub(":%d+$", ""):lower()
  if hote == "localhost" or hote == "127.0.0.1" then return nil end
  return "Adresse du site en http:// refusée : le jeton de lecture partirait en clair. Utilisez https:// " ..
    "(http:// est accepté seulement pour localhost et 127.0.0.1)."
end

local function probleme()
  return probleme_site or probleme_lien
end

local function commence_par_site(url)
  if type(url) ~= "string" or #url < #site then return false end
  if url:sub(1, #site):lower() ~= site:lower() then return false end
  local suite = url:sub(#site + 1, #site + 1)
  return suite == "" or suite == "/" or suite == "?" or suite == "#"
end

local function adresse_panneau()
  return site .. "/panneau.html"
end

-- Le lien Habillage ressemble à <site>/overlay.html?view=<jeton>. On n'en garde que le jeton.
local function analyser_lien(valeur)
  local l = (valeur or ""):gsub("^%s+", ""):gsub("%s+$", "")
  if l == "" then return "", nil, "Collez le lien Habillage depuis la page Sources OBS du site." end
  local j = l:match("[?&]view=([^&#]+)")
  if not j or not j:find("^[%w%-_%.~%%]+$") or #j < 16 then
    return l, nil, "Ce lien ne contient pas de jeton de lecture : recopiez le lien Habillage de la page Sources OBS."
  end
  if not commence_par_site(l) then
    return l, j, "Le lien Habillage ne commence pas par l'adresse du site (" .. site .. ")."
  end
  return l, j, nil
end

------------------------------------------------------------------ rafraîchissement des sources
local function rafraichir(origine)
  local n = 0
  local sources = obs.obs_enum_sources()
  if sources ~= nil then
    for _, src in ipairs(sources) do
      if obs.obs_source_get_unversioned_id(src) == "browser_source" then
        local d = obs.obs_source_get_settings(src)
        local url = obs.obs_data_get_string(d, "url")
        obs.obs_data_release(d)
        if commence_par_site(url) then
          local props = obs.obs_source_properties(src)
          if props ~= nil then
            local bouton = obs.obs_properties_get(props, "refreshnocache")
            if bouton ~= nil then
              obs.obs_property_button_clicked(bouton, src)
              n = n + 1
            end
            obs.obs_properties_destroy(props)
          end
        end
      end
    end
    obs.source_list_release(sources)
  end
  -- Les sources viennent d'être rechargées : la version attendue est maintenant celle à l'écran.
  if version_attendue then version_vue, version_attendue = version_attendue, nil end
  local message = n == 0 and ("Aucune source navigateur de " .. site .. " à rafraîchir.")
    or (n .. " source" .. (n > 1 and "s" or "") .. " rafraîchie" .. (n > 1 and "s" or "") .. " (" .. origine .. ", " .. heure() .. ").")
  journal(obs.LOG_INFO, "[AME Tech] " .. message)
  definir_etat(message, n == 0)
  return n
end

------------------------------------------------------------------ scène Habillage
local function reglages_navigateur(url)
  local d = obs.obs_data_create()
  obs.obs_data_set_string(d, "url", url)
  obs.obs_data_set_bool(d, "is_local_file", false)
  obs.obs_data_set_int(d, "width", 1920)
  obs.obs_data_set_int(d, "height", 1080)
  obs.obs_data_set_bool(d, "shutdown", false)
  obs.obs_data_set_bool(d, "restart_when_active", false)
  obs.obs_data_set_bool(d, "reroute_audio", true)
  return d
end

-- Sur un canevas autre que 1920x1080, la source est mise à l'échelle du canevas.
local function caler(item)
  local ovi = obs.obs_video_info()
  if obs.obs_get_video_info(ovi) and (ovi.base_width ~= 1920 or ovi.base_height ~= 1080) then
    local bornes = obs.vec2()
    bornes.x, bornes.y = ovi.base_width, ovi.base_height
    obs.obs_sceneitem_set_bounds_type(item, obs.OBS_BOUNDS_SCALE_INNER)
    obs.obs_sceneitem_set_bounds(item, bornes)
  end
end

local function creer_scene()
  if not jeton or probleme_lien then
    definir_etat(probleme_lien or "Collez d'abord le lien Habillage.", true)
    return
  end
  local scene_src = obs.obs_get_source_by_name(NOM_SCENE)
  local scene, nouvelle = nil, false
  if scene_src ~= nil then
    scene = obs.obs_scene_from_source(scene_src)
    if scene == nil then
      obs.obs_source_release(scene_src)
      definir_etat("Une source s'appelle déjà « " .. NOM_SCENE .. " » sans être une scène : renommez-la puis recommencez.", true)
      return
    end
  else
    scene, nouvelle = obs.obs_scene_create(NOM_SCENE), true
  end

  local d = reglages_navigateur(lien)
  local navigateur = obs.obs_get_source_by_name(NOM_SOURCE)
  local message
  if navigateur == nil then
    navigateur = obs.obs_source_create("browser_source", NOM_SOURCE, d, nil)
    message = "Scène « " .. NOM_SCENE .. " » prête avec la source « " .. NOM_SOURCE .. " »."
  elseif obs.obs_source_get_unversioned_id(navigateur) ~= "browser_source" then
    obs.obs_source_release(navigateur)
    navigateur = nil
    message = "Une source « " .. NOM_SOURCE .. " » existe déjà sans être une source navigateur : renommez-la."
  else
    -- Déjà là : on remet les bons réglages (le jeton a pu changer), sans créer de doublon.
    obs.obs_source_update(navigateur, d)
    message = "La source « " .. NOM_SOURCE .. " » existait : réglages remis à jour, aucun doublon."
  end
  obs.obs_data_release(d)

  if navigateur ~= nil then
    if obs.obs_scene_find_source(scene, NOM_SOURCE) == nil then
      local item = obs.obs_scene_add(scene, navigateur)
      if item ~= nil then caler(item) end
    end
    obs.obs_source_release(navigateur)
  elseif not message then
    message = "Source navigateur indisponible dans cet OBS."
  end

  if nouvelle then
    -- Rendue un peu plus tard, quand l'interface d'OBS a pris la scène dans sa liste.
    scenes_a_liberer[#scenes_a_liberer + 1] = { scene = scene, quand = os.time() + 2 }
  else
    obs.obs_source_release(scene_src)
  end
  journal(obs.LOG_INFO, "[AME Tech] " .. message)
  definir_etat(message, navigateur == nil)
end

------------------------------------------------------------------ sondage de version
local function rien_a_l_antenne(etat)
  local allumes = {}
  for _, cle in ipairs(CLES_ANTENNE) do
    local section = etat[cle]
    if type(section) ~= "table" then
      allumes[#allumes + 1] = cle .. " (absent)"
    elseif section.on ~= false then
      allumes[#allumes + 1] = cle
    end
  end
  return #allumes == 0, allumes
end

local function verifier_antenne()
  if probleme_site then return end
  if not jeton then
    definir_etat("Nouvelle version " .. version_attendue .. " publiée : lien Habillage manquant, rafraîchissez à la main.", true)
    return
  end
  http.demarrer(site .. "/api/etat?view=" .. jeton, function(code, corps, erreur)
    if code == 401 then
      definir_etat("Jeton du lien Habillage refusé par le site : recopiez le lien depuis la page Sources OBS.", true)
      return
    end
    local etat = code == 200 and json.lire(corps)
    if type(etat) ~= "table" then
      definir_etat("État du site illisible (" .. (erreur or ("HTTP " .. code)) .. ") : rafraîchissement reporté.", true)
      return
    end
    local libre, allumes = rien_a_l_antenne(etat)
    if libre then
      journal(obs.LOG_INFO, "[AME Tech] Nouvelle version " .. version_attendue .. " et rien à l'antenne : rafraîchissement.")
      rafraichir("nouvelle version")
    else
      definir_etat("Nouvelle version " .. version_attendue .. " en attente : à l'antenne (" .. table.concat(allumes, ", ") .. "). Nouvel essai dans " .. SONDAGE_S .. " s.", false)
    end
  end)
end

local function sonder()
  http.demarrer(site .. "/api/version", function(code, corps, erreur)
    local donnees = code == 200 and json.lire(corps)
    local version = type(donnees) == "table" and donnees.version
    if type(version) ~= "string" or version == "" then
      definir_etat("Version du site illisible (" .. (erreur or ("HTTP " .. code)) .. ", " .. heure() .. ").", true)
      return
    end
    if version_vue == nil then
      version_vue = version
      definir_etat("Version " .. version .. " à l'écran, vérifiée à " .. heure() .. ".", false)
    elseif version ~= version_vue then
      version_attendue = version
    elseif not version_attendue then
      definir_etat("Version " .. version .. " à l'écran, vérifiée à " .. heure() .. ".", false)
    end
    if version_attendue then verifier_antenne() end
  end)
end

local function tic()
  local maintenant = os.time()
  for i = #scenes_a_liberer, 1, -1 do
    if scenes_a_liberer[i].quand <= maintenant then
      obs.obs_scene_release(scenes_a_liberer[i].scene)
      table.remove(scenes_a_liberer, i)
    end
  end
  if not http.dispo then return end
  if http.en_cours then http.pomper() return end
  if probleme_site then return end
  if maintenant >= prochain_sondage then
    prochain_sondage = maintenant + SONDAGE_S
    sonder()
  end
end

------------------------------------------------------------------ interface du script
local function maj_infos(settings)
  obs.obs_data_set_string(settings, "adresse_panneau", adresse_panneau())
  obs.obs_data_set_string(settings, "etat_script", probleme() or etat_texte)
end

local function lire_reglages(settings)
  local ancien_site = site
  site = nettoyer_site(obs.obs_data_get_string(settings, "site"))
  probleme_site = verifier_site(site)
  lien, jeton, probleme_lien = analyser_lien(obs.obs_data_get_string(settings, "lien_habillage"))
  if site ~= ancien_site then
    version_vue, version_attendue, prochain_sondage = nil, nil, 0
  end
  if not http.dispo then
    etat_texte, etat_alerte = "Rafraîchissement automatique indisponible sur ce poste (" .. (http.raison or "?") .. ") : utilisez le bouton ou le raccourci.", true
  end
  maj_infos(settings)
end

function script_description()
  return "<h3>AME Tech pour OBS</h3>" ..
    "<p>Habillage du site de régie : création de la scène, rafraîchissement des sources, panneau en dock.</p>" ..
    "<p>Panneau à ajouter dans <b>Docks &gt; Docks de navigateur personnalisés</b> : l'adresse s'affiche plus bas " ..
    "(par défaut <code>" .. SITE_DEFAUT .. "/panneau.html</code>).</p>" ..
    "<p style='color:#888'>Version " .. VERSION_SCRIPT .. "</p>"
end

function script_defaults(settings)
  obs.obs_data_set_default_string(settings, "site", SITE_DEFAUT)
  obs.obs_data_set_default_string(settings, "lien_habillage", "")
end

function script_properties()
  local props = obs.obs_properties_create()

  local p_site = obs.obs_properties_add_text(props, "site", "Adresse du site", obs.OBS_TEXT_DEFAULT)
  obs.obs_property_set_long_description(p_site, "Par défaut " .. SITE_DEFAUT .. ". Sert aussi à reconnaître les sources navigateur à rafraîchir. https:// obligatoire, sauf localhost et 127.0.0.1.")
  local p_lien = obs.obs_properties_add_text(props, "lien_habillage", "Lien Habillage", obs.OBS_TEXT_PASSWORD)
  obs.obs_property_set_long_description(p_lien, "Copiez le lien « Habillage » sur la page Sources OBS du site et collez-le ici. Il contient un jeton de lecture, gardé dans les réglages d'OBS de ce poste.")

  obs.obs_properties_add_button(props, "creer_scene", "Créer la scène Habillage", function()
    creer_scene()
    if mes_reglages ~= nil then maj_infos(mes_reglages) end
    return true
  end)
  obs.obs_properties_add_button(props, "rafraichir", "Rafraîchir l'habillage", function()
    rafraichir("bouton")
    if mes_reglages ~= nil then maj_infos(mes_reglages) end
    return true
  end)

  local p_panneau = obs.obs_properties_add_text(props, "adresse_panneau", "Panneau (dock)", obs.OBS_TEXT_INFO)
  obs.obs_property_set_long_description(p_panneau, "Dans OBS : Docks > Docks de navigateur personnalisés, nom « AME Panneau », collez cette adresse, puis connectez-vous avec le code à 6 chiffres.")
  local p_etat = obs.obs_properties_add_text(props, "etat_script", "État", obs.OBS_TEXT_INFO)
  if probleme() or etat_alerte then obs.obs_property_text_set_info_type(p_etat, obs.OBS_TEXT_INFO_WARNING) end

  local function modifie(proprietes, _, settings)
    lire_reglages(settings)
    local info = obs.obs_properties_get(proprietes, "etat_script")
    if info ~= nil then
      obs.obs_property_text_set_info_type(info, (probleme() or etat_alerte) and obs.OBS_TEXT_INFO_WARNING or obs.OBS_TEXT_INFO_NORMAL)
    end
    return true
  end
  obs.obs_property_set_modified_callback(p_site, modifie)
  obs.obs_property_set_modified_callback(p_lien, modifie)
  return props
end

function script_update(settings)
  lire_reglages(settings)
end

function script_load(settings)
  mes_reglages = settings
  raccourci = obs.obs_hotkey_register_frontend("ame_tech_rafraichir", "Rafraîchir l'habillage (AME Tech)", function(appui)
    if appui then rafraichir("raccourci") end
  end)
  local tableau = obs.obs_data_get_array(settings, "ame_tech_rafraichir_raccourci")
  obs.obs_hotkey_load(raccourci, tableau)
  obs.obs_data_array_release(tableau)
  lire_reglages(settings)
  obs.timer_add(tic, 250)
  journal(obs.LOG_INFO, "[AME Tech] Script " .. VERSION_SCRIPT .. " chargé. Panneau à ajouter en dock : " .. adresse_panneau() ..
    (http.dispo and "" or (" ; rafraîchissement automatique indisponible (" .. tostring(http.raison) .. ")")))
end

function script_save(settings)
  if raccourci ~= nil then
    local tableau = obs.obs_hotkey_save(raccourci)
    obs.obs_data_set_array(settings, "ame_tech_rafraichir_raccourci", tableau)
    obs.obs_data_array_release(tableau)
  end
end

function script_unload()
  obs.timer_remove(tic)
  for _, s in ipairs(scenes_a_liberer) do obs.obs_scene_release(s.scene) end
  scenes_a_liberer = {}
  if http.dispo then http.fermer() end
end

-- Accès réservé aux essais hors OBS (tests/), sans effet dans OBS.
AMETECH_INTERNE = { json = json, http = http, analyser_lien = analyser_lien, nettoyer_site = nettoyer_site, verifier_site = verifier_site,
  rien_a_l_antenne = rien_a_l_antenne, tic = tic, rafraichir = rafraichir, creer_scene = creer_scene,
  etat = function() return { texte = etat_texte, alerte = etat_alerte, version_vue = version_vue, version_attendue = version_attendue, site = site, jeton_present = jeton ~= nil } end,
  forcer_sondage = function() prochain_sondage = 0 end }
