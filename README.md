# Prospect Bot — Meta Ads → Discord

Cherche quotidiennement des startups qui tournent des pubs Meta avec des
créas faibles (image uniquement, peu de variantes = petit compte) et poste
la liste sur Discord.

**Scope volontairement restreint à des applications mobiles iOS et/ou
Android** — jamais des SaaS/sites accessibles uniquement par navigateur.
Ce filtre marche en deux couches : un pré-filtre par mots-clés (mention
d'App Store, Google Play, "download the app"...) puis une confirmation par
Claude qui lit le texte de la pub et tranche vraiment. Chaque message posté
sur Discord ne contient que le nom du produit et le lien direct vers la pub
sur l'Ad Library — jamais de site web ni d'email : ces champs n'existent
plus dans le code (ils faisaient partie d'une ancienne version, voir plus
bas).

## ⚠️ Limite à connaître

L'API Ad Library de Meta ne donne des données fiables sur les **pubs
commerciales** que pour les pays de l'**UE** (post-DSA). Hors UE, seules les
pubs politiques/enjeux de société sont accessibles. Ce bot est donc scopé
UE (`FR, DE, ES, IT, NL, BE, PT, IE, AT, PL, SE, DK, FI, LU` par défaut,
modifiable dans `prospect_bot.py`).

## Mise en place (une seule fois)

### 1. Obtenir un access token Meta
1. Va sur [developers.facebook.com](https://developers.facebook.com), crée une app (type "Business").
2. Confirme ton identité sur [facebook.com/ID](https://facebook.com/ID) (requis par Meta pour l'accès à l'Ad Library API).
3. Génère un access token depuis l'outil "Graph API Explorer" ou via ton app.
   → C'est la valeur de `META_ACCESS_TOKEN`.

### 2. Créer un webhook Discord
1. Dans ton serveur Discord → Paramètres du salon `#prospects-du-jour` → Intégrations → Webhooks → Nouveau webhook.
2. Copie l'URL.
   → C'est la valeur de `DISCORD_WEBHOOK_URL`.

### 3. Clé API Anthropic (pour la rédaction des emails)
1. Récupère une clé sur [console.anthropic.com](https://console.anthropic.com) → API Keys.
   → C'est la valeur de `ANTHROPIC_API_KEY`.
2. Optionnel : si cette clé n'est pas fournie, le bot fonctionne quand même
   (il liste juste les prospects sans email pré-rédigé).

### 4. Héberger le script (GitHub Actions, gratuit)
1. Push ce dossier dans un repo GitHub (public ou privé, peu importe).
2. Dans le repo : Settings → Secrets and variables → Actions → New repository secret.
   - Ajoute `META_ACCESS_TOKEN`
   - Ajoute `DISCORD_WEBHOOK_URL`
   - Ajoute `ANTHROPIC_API_KEY`
3. C'est tout — le workflow `.github/workflows/daily-prospects.yml` tourne
   automatiquement tous les jours à 6h UTC. Tu peux aussi le lancer à la
   main depuis l'onglet "Actions" du repo (bouton "Run workflow").

## Réglages à ajuster (`prospect_bot.py`)

| Variable | Rôle |
|---|---|
| `SEARCH_TERMS` | Mots-clés recherchés (`app`, `SaaS`, `startup`...) |
| `AD_REACHED_COUNTRIES` | Pays UE ciblés |
| `MAX_ACTIVE_ADS_PER_PAGE` | Seuil pour exclure les "gros comptes" (défaut: 8) |
| `TARGET_MEDIA_TYPES` | `["IMAGE", "MEME"]` par défaut pour cibler les créas pauvres — n'a aucun rapport avec le filtre iOS/Android, voir `MOBILE_APP_KEYWORDS` pour ça |
| `MOBILE_APP_KEYWORDS` / `NEGATIVE_MOBILE_APP_PHRASES` | Pré-filtre (couche 1) qui ne garde que les pubs mentionnant un signal d'app mobile installable |

## Tester en local

```bash
pip install -r requirements.txt
export META_ACCESS_TOKEN="..."
export DISCORD_WEBHOOK_URL="..."
python prospect_bot.py
```

## Limites connues à garder en tête

- **Pas de site web ni d'email**: le bot ne fait plus aucune tentative pour deviner
  un domaine ou scraper un email de contact (une ancienne version essayait, ce
  n'est plus le cas). Le message Discord se limite au nom du produit et au lien
  `ad_snapshot_url` fourni par Meta — si tu veux le site ou un contact, c'est la
  pub elle-même (ouverte via ce lien) qu'il faut consulter à la main.

- **Filtre "app mobile" imparfait par nature**: la couche 1 (mots-clés App
  Store/Google Play) peut rater une vraie app qui ne mentionne aucun de ces
  termes dans le texte de sa pub — elle serait exclue à tort avant même
  d'atteindre Claude. La couche 2 (Claude) est plus fiable sur les cas
  ambigus mais dépend de la qualité du texte de pub disponible. En pratique,
  la quasi-totalité des pubs d'app contiennent un CTA de téléchargement
  explicite, donc l'effet est marginal.

- **Rate limit**: ~200 appels/heure côté Meta. Avec 3 mots-clés et une
  pagination raisonnable, largement suffisant pour un run quotidien.
- **"Gros compte" = proxy imparfait**: on n'a pas accès au nombre de
  followers de la page via ce token, donc le filtre se base sur le nombre
  de pubs actives. Une grosse boite qui débute une seule campagne test
  pourrait passer le filtre — à trier à l'œil ensuite.
- **Qualité de la vidéo non évaluée**: l'API ne permet pas de juger si une
  vidéo est "un peu nulle" — seulement d'exclure les comptes qui n'ont AUCUNE
  vidéo. Une étape suivante possible: envoyer les `ad_snapshot_url` à un
  modèle vision pour un vrai jugement qualité (dispo si tu veux qu'on
  l'ajoute).
