#!/usr/bin/env python3
"""
Prospect Bot — Meta Ad Library -> Discord

Cherche des pubs actives sur Meta (FB/IG) autour de mots-clés SaaS/app,
filtre pour ne garder que les annonceurs "petits" et visuellement faibles
(peu de pubs actives, pas de vidéo), et poste un digest quotidien sur Discord.

Limite importante de l'API Meta (voir README) : la recherche par mot-clé sur
des pubs COMMERCIALES n'est fiable que pour les pays de l'UE (post-DSA).
Ce script est donc scopé UE par défaut — cohérent avec un ciblage de
startups francaises/européennes.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

META_ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
# Nécessaire uniquement si ta clé API est de type "Personnel"/identity-linked (Console
# Anthropic) plutôt qu'une clé classique de workspace. Trouvable dans les paramètres
# du workspace sur console.anthropic.com (identifiant du type "wrkspc_...").
ANTHROPIC_WORKSPACE_ID = os.environ.get("ANTHROPIC_WORKSPACE_ID")

ANTHROPIC_MODEL = "claude-sonnet-5"

# Mode démo : poste un exemple avec un prospect fictif sur Discord, sans toucher à
# l'API Meta. Utile pour montrer le rendu avant que le token Meta soit débloqué, ou
# pour vérifier le rendu après un changement de prompt. Activé via DEMO_MODE=1.
DEMO_MODE = os.environ.get("DEMO_MODE") == "1"

DEMO_PROSPECT_ADS = [{
    "page_name": "Nooks (EXEMPLE FICTIF)",
    "page_id": "demo-0000",
    "ad_creative_bodies": ["Nooks, the easiest way to organize your notes. Download free today."],
    "ad_creative_link_captions": ["nooks-demo.app"],
    "ad_snapshot_url": "https://www.facebook.com/ads/library/?id=0000000000",
    "_media_type": "IMAGE",
}]

GRAPH_VERSION = "v21.0"
AD_LIBRARY_URL = f"https://graph.facebook.com/{GRAPH_VERSION}/ads_archive"

# Mots-clés à surveiller. Élargi volontairement pour maximiser le nombre de
# comptes scannés (plus de mots-clés = plus de pubs remontées par l'API) —
# le filtrage qualité se fait ensuite via filter_small_advertisers /
# filter_out_of_scope, pas ici. Ajuste au fil de l'eau selon ce qui convertit.
SEARCH_TERMS = [
    "app", "SaaS", "startup", "mobile app", "web app", "platform",
    "dashboard", "tool", "software", "marketplace", "subscription",
    "download the app", "sign up free", "try it free", "new app",
]

# Pays UE (là où l'API donne des données commerciales fiables)
AD_REACHED_COUNTRIES = [
    "FR", "DE", "ES", "IT", "NL", "BE", "PT", "IE", "AT",
    "PL", "SE", "DK", "FI", "LU",
]

# Un annonceur avec plus de X pubs actives sur la période = probablement
# une boite qui a déjà une équipe growth/ads => on l'exclut. Relevé de 5 à 8
# pour laisser passer plus de prospects sans perdre le signal "petit compte".
MAX_ACTIVE_ADS_PER_PAGE = 8

# Plafond de pages parcourues par recherche (100 résultats/page). Les mots-clés
# larges ("app", "startup"...) peuvent matcher des milliers de pubs sur 14 pays UE ;
# sans plafond, une seule recherche peut prendre 20-30 min à paginer entièrement.
# 5 pages = 500 résultats, largement suffisant pour repérer des petits comptes
# (les gros volumes de résultats sont de toute façon dominés par des annonceurs
# établis qu'on filtre ensuite via MAX_ACTIVE_ADS_PER_PAGE).
MAX_PAGES_PER_SEARCH = 5

# Plafond du nombre de NOUVEAUX prospects enrichis (email + rédaction) et
# postés sur Discord par run. Sans ça, un run peut remonter plusieurs milliers de
# pages "petit compte" (≤ 8 pubs actives est un filtre large, pas rare du tout) et
# spammer Discord de milliers de messages en une seule fois, en épuisant le crédit
# API Anthropic au passage. Le fichier seen_pages.json garantit que les prospects
# non traités ce run-ci ne sont jamais perdus : ils remontent au run suivant tant
# qu'ils ne sont pas encore dans "seen". Avec 3 runs/jour, ça fait jusqu'à
# 3 x MAX_PROSPECTS_PER_RUN prospects qualifiés traités par jour.
MAX_PROSPECTS_PER_RUN = 15

# On priorise les comptes qui ne tournent QUE de l'image ou du meme (pas de
# vidéo du tout) comme signal de créa faible / pas d'UGC.
TARGET_MEDIA_TYPES = ["IMAGE", "MEME"]

# Ne jamais démarcher les apps finance/banque/crypto/investissement (réglementation,
# cible différente, hors périmètre voulu). Heuristique par mot-clé sur le nom de page
# et le texte de la pub - imparfait mais suffisant pour exclure les cas évidents.
FINANCE_KEYWORDS = [
    "bank", "banque", "budget", "finance", "financial", "invest", "investing",
    "investment", "crypto", "bitcoin", "wallet", "trading", "trader", "loan",
    "credit", "credit card", "carte bancaire", "épargne", "savings", "fintech",
    "payment", "paiement", "insurance", "assurance", "tax", "impôt", "pret",
    "prêt", "neobank", "néobanque",
]

# Le ciblage par mot-clé large ("app", "tool", "platform"...) matche aussi
# énormément de commerces/contenus qui n'ont rien d'une app ou d'un SaaS tech
# (boutiques e-commerce, blogs bien-être, fermes de contenu romans, commerces
# locaux...) simplement parce qu'ils réutilisent ces mots dans leur pub. On
# exclut ces catégories hors périmètre pour ne garder que des vraies cibles
# business/tech. Heuristique par mot-clé, imparfaite mais suffisante pour
# éliminer le plus gros du bruit observé.
OUT_OF_SCOPE_KEYWORDS = [
    # e-commerce / mode / beauté
    "store", "boutique", "clothing", "clothes", "fashion", "dress", "shoes",
    "jewelry", "bijoux", "skincare", "cosmetic", "makeup", "beauty", "perfume",
    "shop now", "collection",
    # santé / bien-être / lifestyle personnel
    "wellness", "yoga", "meditation", "therapy", "therapist", "massage",
    "diet", "workout", "fitness coach", "nutrition", "blood pressure",
    "cholesterol", "diabetes", "arthritis", "supplement", "vitamin", "peptide",
    # contenu / divertissement / romans
    "novel", "romance", "story", "storyline", "fiction", "reading app",
    "comic", "manga",
    # commerces locaux / services physiques
    "restaurant", "ristorante", "trattoria", "hotel", "clinic", "clinique",
    "veterinar", "pet care", "boat", "yacht", "auto export", "car export",
    "real estate", "immobilier", "furniture", "decor", "cleaning", "laundry",
    # rencontre / coaching perso
    "dating", "relationship advisor", "life coach", "astrology", "horoscope",
]


def matches_keywords(page_name: str, ads: list[dict], keywords: list[str]) -> bool:
    text = page_name.lower()
    for ad in ads:
        text += " " + " ".join(ad.get("ad_creative_bodies", [])).lower()
        text += " " + " ".join(ad.get("ad_creative_link_titles", [])).lower()
    return any(keyword in text for keyword in keywords)

# Fichier qui garde la trace des pages déjà signalées, pour ne pas spammer
# le même prospect tous les jours. Committé dans le repo par le workflow.
SEEN_FILE = Path(__file__).parent / "seen_pages.json"

FIELDS = ",".join([
    "id",
    "page_id",
    "page_name",
    "ad_creative_bodies",
    "ad_creative_link_captions",
    "ad_creative_link_titles",
    "ad_delivery_start_time",
    "ad_snapshot_url",
    "publisher_platforms",
])


# ---------------------------------------------------------------------------
# Meta Ad Library
# ---------------------------------------------------------------------------

def fetch_ads(search_term: str, media_type: str) -> list[dict]:
    """Récupère toutes les pubs actives pour un mot-clé donné, avec pagination."""
    if not META_ACCESS_TOKEN:
        print("ERREUR: META_ACCESS_TOKEN manquant.", file=sys.stderr)
        sys.exit(1)

    params = {
        "search_terms": search_term,
        "ad_type": "ALL",
        "ad_active_status": "ACTIVE",
        "ad_reached_countries": json.dumps(AD_REACHED_COUNTRIES),
        "media_type": media_type,
        "fields": FIELDS,
        "limit": 100,
        "access_token": META_ACCESS_TOKEN,
    }

    results = []
    url = AD_LIBRARY_URL
    use_params = params
    pages_fetched = 0

    while url and pages_fetched < MAX_PAGES_PER_SEARCH:
        resp = requests.get(url, params=use_params, timeout=30)

        if resp.status_code == 429:
            print("Rate limit atteint, pause 60s...")
            time.sleep(60)
            continue

        if resp.status_code != 200:
            print(f"Erreur API ({resp.status_code}) pour '{search_term}': {resp.text[:300]}", file=sys.stderr)
            break

        data = resp.json()
        for ad in data.get("data", []):
            ad["_media_type"] = media_type  # tag pour l'adaptation du message
        results.extend(data.get("data", []))
        pages_fetched += 1

        next_url = data.get("paging", {}).get("next")
        url = next_url
        use_params = None  # l'URL "next" contient déjà tous les params

    if url and pages_fetched >= MAX_PAGES_PER_SEARCH:
        print(f"  (plafond de {MAX_PAGES_PER_SEARCH} pages atteint pour '{search_term}', reste ignoré)")

    return results


# ---------------------------------------------------------------------------
# Filtrage
# ---------------------------------------------------------------------------

def group_by_page(ads: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for ad in ads:
        page_id = ad.get("page_id")
        if not page_id:
            continue
        grouped.setdefault(page_id, []).append(ad)
    return grouped


def filter_small_advertisers(grouped: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Ne garde que les pages avec peu de pubs actives (proxy pour 'petit compte')."""
    return {
        page_id: ads
        for page_id, ads in grouped.items()
        if len(ads) <= MAX_ACTIVE_ADS_PER_PAGE
    }


def filter_out_of_scope(grouped: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Exclut finance/banque/crypto ET les commerces/contenus hors périmètre
    business/tech (mode, beauté, romans, commerces locaux, etc.)."""
    kept = {}
    for page_id, ads in grouped.items():
        page_name = ads[0].get("page_name", "")
        if matches_keywords(page_name, ads, FINANCE_KEYWORDS):
            print(f"  Exclu (finance) : {page_name}")
            continue
        if matches_keywords(page_name, ads, OUT_OF_SCOPE_KEYWORDS):
            print(f"  Exclu (hors périmètre business/tech) : {page_name}")
            continue
        kept[page_id] = ads
    return kept


def dominant_media_type(ads: list[dict]) -> str:
    """Détermine si la page tourne surtout de l'image, surtout du meme, ou un mix."""
    types = [ad.get("_media_type") for ad in ads]
    if all(t == "MEME" for t in types):
        return "MEME"
    if all(t == "IMAGE" for t in types):
        return "IMAGE"
    return "MIX"  # mélange image + meme


def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set[str]) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2))


# ---------------------------------------------------------------------------
# Rédaction d'email personnalisé (API Anthropic)
# ---------------------------------------------------------------------------

EMAIL_SYSTEM_PROMPT = """Tu rédiges des emails de prospection à froid pour Tanto Lab, \
signés par Thomas. L'email généré (objet ET corps) doit être écrit intégralement en \
ANGLAIS, quelle que soit la langue du texte de la pub fourni en entrée.

Contexte business (à respecter strictement) :
- Tanto Lab est une agence de production vidéo UGC (script, tournage, montage) \
spécialisée dans les apps, produits web et SaaS early-stage (pre-seed à seed).
- Deux personnes : Anton (créa : script, tournage, montage) et Thomas (prospection).
- Tanto Lab est elle-même une jeune structure qui démarre, pas une grosse agence \
établie. C'est un fait assumé, pas une excuse : ça justifie l'offre ci-dessous plutôt \
que de rester caché ou de sonner comme une réduction bradée.
- Process : 1) Brief (produit, audience, angle) 2) Script sur mesure, validé par le \
client avant tournage 3) Tournage par un créateur vérifié + montage pro, livré prêt \
à poster en pub.
- Tarifs standards : 100$ la vidéo à l'unité. Pack de 4 vidéos/mois à 360$ (90$/vidéo, \
best value). Volume au-delà : sur devis.
- OFFRE DE PROSPECTION À FROID (à utiliser dans l'email, remplace la simple mention \
du tarif standard) : premier essai à prix coûtant, 100$, sans marge. Garantie : si la \
vidéo n'obtient pas un meilleur CTR (click-through rate) que leur créa actuelle sur 2 \
semaines, remboursement, sans discussion. La métrique de comparaison doit être le CTR, \
explicitement nommé dans l'email, jamais un mot vague comme "outperform" ou "better \
results" tout seul (le CTR se lit directement dans Meta Ads Manager côté prospect, et \
se stabilise assez vite pour être fiable sur 2 semaines, contrairement au ROAS ou aux \
conversions qui demandent plus de volume). En échange, Tanto Lab demande le droit de \
partager les vrais résultats ensuite (chiffres, pas juste un avis). Cette offre existe \
précisément parce que Tanto Lab n'a pas encore de case study à montrer, il faut le \
dire simplement, sans s'excuser.
- Cible : fondateur solo ou petite équipe, souvent technique, sans marketing interne, \
budget serré. Il a peur de se faire arnaquer par une agence chère et générique.
- Le prospect a été repéré parce qu'il tourne actuellement une pub Meta (donc il a \
un budget ads) mais avec une créa faible : image statique, pas de vidéo, pas d'UGC.

Structure obligatoire en 9 blocs (contenu en français ici, sortie en anglais) — CHAQUE \
bloc numéroté ci-dessous correspond à UN paragraphe séparé par un saut de ligne double \
dans le JSON final, jamais fusionné avec le bloc suivant :
0. Une courte formule d'introduction avant l'accroche (ex : "Hey," ou "Hi,"), jamais \
"Dear Sir/Madam" ni de formalisme excessif. Son propre bloc, séparé de l'accroche.
1. Ouverture contextuelle + compliment bref : commence par comment tu es tombé sur eux \
de façon naturelle et jamais ciblée ou intrusive (ex : "I've been checking out apps \
running Meta ads lately, and yours caught my eye" ou variante proche) — jamais une \
observation trop précise qui donne l'impression d'avoir été traqué. Enchaîne avec une \
reconnaissance sincère et courte du produit (une phrase, pas plus), sans en faire trop \
ni sonner comme un compliment creux ("great value prop, love it"). Ce bloc ne mentionne \
PAS encore le problème créa : c'est une ouverture, pas une critique.
2. Transition + problème concret : ouvre par une formule de transition douce qui \
signale un point à soulever sans être frontal (ex : "One thing I noticed though" ou \
variante naturelle proche), puis nomme le vrai problème que révèle leur créa actuelle. \
Le type de créa dominant (IMAGE, MEME ou MIX) t'est donné en amont du texte de la pub, \
adapte l'angle du problème en conséquence, ne réutilise jamais le même angle pour les \
trois :
   - IMAGE : photo ou capture d'écran statique, rien ne bouge, impossible de montrer \
le produit en action ni de créer de la confiance par une vraie personne qui l'utilise.
   - MEME : format template humoristique, ça capte peut-être l'œil une fois mais ça \
ne construit aucune confiance pour télécharger une app ou payer un SaaS, ça fait \
amateur sur ce type de produit précisément parce que le format vient d'ailleurs (DTC, \
mode) et ne parle pas de ce que fait vraiment le produit.
   - MIX : un mélange image et meme sans ligne directrice claire, signe d'une créa \
qui se fait au coup par coup plutôt que testée et itérée.
Ce bloc reste sur le CONSTAT (ce qui cloche dans leur pub), sans encore expliquer \
pourquoi l'UGC résout ça, ça vient dans le bloc suivant.
3. Justification UGC : explique en une ou deux phrases pourquoi l'UGC résout le \
problème du bloc 2, en t'appuyant sur ce FAIT VÉRIFIÉ (utilise-le tel quel ou \
reformulé naturellement, ne l'utilise pas dans CHAQUE email pour ne pas être \
répétitif, un email sur deux environ suffit) : selon une étude Nielsen ("Global Trust \
in Advertising"), 92% des consommateurs font davantage confiance aux recommandations \
de pairs et au bouche-à-oreille qu'à la publicité de marque classique, ce qui explique \
pourquoi l'UGC convertit mieux qu'un visuel produit par la marque elle-même. N'invente \
JAMAIS d'autre chiffre, d'autre étude, ou d'autre source. Si tu n'utilises pas ce \
chiffre dans un email donné, reste sur une explication qualitative sans donnée \
chiffrée inventée. Le mot "UGC" doit apparaître explicitement, en toutes lettres, \
jamais remplacé par une paraphrase comme "real person content" ou "a real person \
using the app".
4. Présentation + offre : "I'm Thomas, I handle outreach for Tanto Lab." (ou variante \
naturelle proche), puis une phrase indiquant que Tanto Lab crée des vidéos UGC faites \
pour performer, puis le fait que Tanto Lab démarre encore, puis l'annonce du prix \
(essai à prix coûtant, 100$, sans marge). PAS de détail sur le process (pas de mention \
de script, de créateur, de tournage, de montage) : ça alourdit le mail, on saute \
direct de la présentation à l'offre. S'arrête après le prix.
5. Garantie : uniquement la garantie CTR (si la vidéo n'obtient pas un meilleur CTR \
que leur créa actuelle sur 2 semaines, remboursement). Formule directe, sans ajouter \
"no questions asked" ni formule de renforcement similaire, la garantie parle d'elle-même.
6. Contrepartie : uniquement la demande en échange (le droit de partager les vrais \
résultats ensuite). Bloc séparé de la garantie, jamais fusionné.
7. Appel à l'action + signature : une seule question ouverte, facile à répondre par \
oui/non, JAMAIS une formulation en ordre ou en impératif ("Reply and let me know if \
you want to try it out" est strictement interdit — ça sonne comme une instruction, pas \
une invitation). Formule-la toujours comme une vraie question, courte, du type "Want \
to give it a shot?" ou "Worth a try?" ou variante naturelle proche, jamais à proposer \
un call ou un rendez-vous, jamais vague ("feel free to reach out"). La signature \
("Thomas, Tanto Lab") suit, dans le même bloc ou son propre bloc.

Ton et style (règles strictes, sans exception, appliquées au texte ANGLAIS généré) :
- Direct, sans jargon d'agence ("boost your engagement with our 360 expertise")
- Friendly et chaleureux, pas cassant : on pointe un problème pour aider, pas pour \
rabaisser. Commence par une remarque positive ou neutre sur ce qu'ils font avant \
d'introduire le problème (point 1), et garde un ton d'égal à égal, curieux, jamais \
condescendant. "Direct" veut dire clair et concret, pas froid ni sec.
- Première personne ("I saw", "I can"), jamais de "we" corporate — assumer la petite \
taille comme un atout (réactivité, pas de comité de validation)
- Un chiffre ou un fait concret vaut mieux qu'un adjectif vague
- Une pointe d'auto-dérision sur la culture startup est bienvenue si ça sonne naturel \
(burn rate, pivots, half-baked MVP), sans en abuser
- Aucun tiret cadratin (em dash "—"), nulle part, y compris dans la signature (pas de \
"Thomas — Tanto Lab" : écrire "Thomas, Tanto Lab" ou sur deux lignes séparées)
- Jamais la formule "it's not X, it's Y" ni ses variantes
- Aucune précaution inutile ("I think that", "it seems that", "maybe") : affirme ou \
tais-toi, mais reste chaleureux dans le ton, pas juste dans le vocabulaire
- Pas d'emoji, pas de gras décoratif, pas de formules creuses ("hope you're doing well")
- Casse les énumérations de trois éléments quand deux suffisent
- Varie la longueur des phrases et des paragraphes, alterne très court et plus développé
- Mots bannis : "leverage", "unlock", "seamless", "elevate", "game-changer"
- Pour le CTA (point 7) : toujours une question ouverte, jamais un ordre ou une \
instruction ("Reply and let me know..." interdit). INTERDIT de proposer un call, un \
rendez-vous ou un créneau, sous quelque forme que ce soit ("quick call", "jump on a \
call", "worth a chat", "hop on a 10-min call"...). Varie la formulation à chaque email \
pour ne jamais répéter le même patron : par exemple demander si un format donné les \
intéresse, proposer d'envoyer 2-3 exemples similaires en réponse, ou demander une info \
précise sur leur projet. Une phrase, jamais plus.
- Court : 110-150 mots pour le corps (intro incluse)
- Format obligatoire du champ "body" : chacun des 9 blocs de la structure ci-dessus \
(0, 1, 2, 3, 4, 5, 6, 7) est son propre paragraphe, séparé du suivant par un saut de \
ligne double ("\\n\\n" dans le JSON), jamais fusionné avec un autre, jamais un pavé de \
texte compact. Chaque bloc reste court (1-2 phrases), jamais un paragraphe de 3+ \
phrases.

Rappel : "subject" et "body" doivent être rédigés entièrement en anglais. Le champ \
"problem" ci-dessous, lui, doit être écrit en FRANÇAIS (usage interne pour l'équipe).

Réponds UNIQUEMENT avec un JSON de la forme :
{"product_name": "...", "problem": "...", "subject": "...", "body": "..."}
où "product_name" est le nom du produit/app/service tel qu'il apparaît dans le texte \
de la pub (pas le nom de la page Facebook, qui peut être un nom de personne si la pub \
tourne depuis un profil perso). Cherche un nom de marque/produit explicite dans le \
texte de la pub fourni. Si aucun nom de produit clair n'apparaît dans le texte, mets \
null pour ce champ plutôt que d'inventer un nom.
"problem" est UNE seule phrase courte en français qui résume le problème créa \
détecté chez ce prospect (le même problème que celui développé au point 2 de l'email, \
condensé en une phrase factuelle, sans "je pense que" ni formule floue). Exemple : \
"Meme unique recyclé sur 3 pubs, aucune preuve produit, aucune voix humaine."
Pas de texte avant ou après le JSON, pas de balises markdown."""


def draft_email(page_name: str, ad_bodies: list[str], media_type: str) -> dict | None:
    """Génère un email de prospection personnalisé via l'API Anthropic."""
    if not ANTHROPIC_API_KEY:
        return None

    ad_text = "\n---\n".join(b for b in ad_bodies if b) or "(texte de pub non disponible)"

    user_prompt = (
        f"Prospect : {page_name}\n"
        f"Type de créa dominant : {media_type}\n\n"
        f"Texte(s) de leur pub Meta actuelle :\n{ad_text}\n\n"
        f"Rédige l'email de prospection."
    )

    try:
        headers = {
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if ANTHROPIC_WORKSPACE_ID:
            headers["anthropic-workspace-id"] = ANTHROPIC_WORKSPACE_ID

        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 1500,
                "thinking": {"type": "disabled"},
                "system": EMAIL_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_prompt}],
            },
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"Erreur API Anthropic ({resp.status_code}) pour '{page_name}': {resp.text[:500]}", file=sys.stderr)
            return None
        data = resp.json()
        text = "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        )
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        if not text:
            print(f"Réponse Anthropic vide pour '{page_name}'. stop_reason={data.get('stop_reason')}, réponse brute (tronquée): {json.dumps(data)[:800]}", file=sys.stderr)
            return None
        return json.loads(text)
    except Exception as e:  # noqa: BLE001 - on ne veut pas planter tout le run pour un email raté
        print(f"Erreur génération email pour '{page_name}': {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

BOT_NAME = "Celestin"


def post_to_discord(prospects: list[tuple[str, list[dict], dict | None, str, str | None, str | None]], demo: bool = False) -> None:
    if not DISCORD_WEBHOOK_URL:
        print("ERREUR: DISCORD_WEBHOOK_URL manquant.", file=sys.stderr)
        sys.exit(1)

    if not prospects:
        payload = {
            "username": BOT_NAME,
            "content": f"🔍 {BOT_NAME} n'a trouvé aucun nouveau prospect aujourd'hui (image/meme, petits comptes, UE).",
        }
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        return

    if demo:
        intro_text = (
            f"🧪 **EXEMPLE — {BOT_NAME}** (mode démo, prospect fictif, pas une vraie pub trouvée)\n"
            f"Ça montre à quoi ressemblera un vrai post une fois l'accès Meta débloqué."
        )
    else:
        intro_text = f"🎯 **{BOT_NAME} a trouvé {len(prospects)} nouveaux prospects aujourd'hui**"

    intro = {"username": BOT_NAME, "content": intro_text}
    requests.post(DISCORD_WEBHOOK_URL, json=intro, timeout=15)

    # Un seul message texte simple par prospect (pas d'embed) : Nom / Ad / Message
    for page_id, ads, email, media_type, website, contact_email in prospects:
        page_name = ads[0].get("page_name", "?")
        snapshot = ads[0].get("ad_snapshot_url", "")

        if email:
            subject = email.get("subject", "?")
            body = email.get("body", "?")
            message_block = f"{subject}\n\n{body}"
            display_name = email.get("product_name") or page_name
        else:
            message_block = "(non généré — ANTHROPIC_API_KEY manquant ou erreur, voir logs)"
            display_name = page_name

        content = (
            f"Nom: {display_name}\n"
            f"Ad: {snapshot or 'N/A'}\n\n"
            f"Message:\n{message_block}"
        )
        if len(content) > 1990:
            content = content[:1980] + "\n…(coupé)"

        requests.post(DISCORD_WEBHOOK_URL, json={"username": BOT_NAME, "content": content}, timeout=15)
        time.sleep(1)  # éviter le rate limit du webhook Discord


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if DEMO_MODE:
        print("DEMO_MODE actif : génération d'un exemple fictif, pas d'appel à l'API Meta.")
        page_name = DEMO_PROSPECT_ADS[0]["page_name"]
        ad_bodies = DEMO_PROSPECT_ADS[0]["ad_creative_bodies"]
        media_type = "IMAGE"
        email = draft_email(page_name, ad_bodies, media_type)
        demo_prospects = [("demo-0000", DEMO_PROSPECT_ADS, email, media_type, None, None)]
        post_to_discord(demo_prospects, demo=True)
        return

    seen = load_seen()
    all_ads: list[dict] = []

    for term in SEARCH_TERMS:
        for media_type in TARGET_MEDIA_TYPES:
            print(f"Recherche: '{term}' ({media_type})...")
            ads = fetch_ads(term, media_type)
            print(f"  -> {len(ads)} pubs trouvées")
            all_ads.extend(ads)

    grouped = group_by_page(all_ads)
    print(f"Total: {len(grouped)} pages annonceurs distinctes")

    grouped = filter_out_of_scope(grouped)
    print(f"Après exclusion finance/hors périmètre: {len(grouped)}")

    small = filter_small_advertisers(grouped)
    print(f"Après filtre 'petit compte' (<= {MAX_ACTIVE_ADS_PER_PAGE} pubs): {len(small)}")

    new_prospects_raw = [(pid, ads) for pid, ads in small.items() if pid not in seen]
    print(f"Nouveaux (jamais signalés): {len(new_prospects_raw)}")

    if len(new_prospects_raw) > MAX_PROSPECTS_PER_RUN:
        print(f"  -> plafonné à {MAX_PROSPECTS_PER_RUN} pour ce run, le reste sera traité au prochain passage")
        new_prospects_raw = new_prospects_raw[:MAX_PROSPECTS_PER_RUN]

    new_prospects = []
    for page_id, ads in new_prospects_raw:
        page_name = ads[0].get("page_name", "?")
        ad_bodies = [b for ad in ads for b in ad.get("ad_creative_bodies", [])]
        media_type = dominant_media_type(ads)

        print(f"  Rédaction email pour '{page_name}' ({media_type})...")
        email = draft_email(page_name, ad_bodies, media_type)
        new_prospects.append((page_id, ads, email, media_type, None, None))

    post_to_discord(new_prospects)

    seen.update(pid for pid, _, _, _, _, _ in new_prospects)
    save_seen(seen)


if __name__ == "__main__":
    main()
