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

# langdetect sert au filtre "anglais uniquement" (voir filter_non_english).
# Import optionnel : si le paquet n'est pas installé (requirements.txt pas à
# jour), on désactive juste ce filtre au lieu de planter tout le script.
try:
    from langdetect import DetectorFactory, LangDetectException, detect
    DetectorFactory.seed = 0  # résultats déterministes entre les runs
except ImportError:
    detect = None
    LangDetectException = Exception

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

# Pubs "reward/cashback" : un modèle d'arnaque/media-buying très répandu sur
# Meta Ads ("Get $15 on us", "Send to PayPal", "claim your reward"...). Ce
# n'est jamais une vraie app early-stage qui a besoin de UGC organique - c'est
# déjà un funnel de performance marketing payant, avec souvent un compte
# annonceur jetable derrière. On exclut peu importe la catégorie déclarée de
# l'app, même si l'app sous-jacente (ex: un compteur de pas) est réelle.
SCAM_REWARD_KEYWORDS = [
    "on us!", "get $", "claim your reward", "claim your prize", "you're eligible",
    "you have been selected", "congratulations you", "reward card", "cash reward",
    "gift card reward", "send to paypal", "paypal reward", "$ reward",
    "spin to win", "scratch to win", "you won", "free gift card",
]

# Apps de fiction interactive/lecture par chapitres (souvent à contenu adulte,
# type ReelShort/DramaBox/GoodNovel). Le texte de pub réel évite parfois les
# mots déjà présents dans OUT_OF_SCOPE_KEYWORDS ("story", "novel"...) en ne
# parlant que de "chapters" - on comble ce trou spécifiquement.
INTERACTIVE_FICTION_KEYWORDS = [
    "more chapters", "next chapter", "unlock chapter", "read to unlock",
    "chapters to read", "full story unlocked", "read the ending",
]

# Heuristique sur le NOM DE PAGE : les comptes de media buying/arnaque à la
# chaîne utilisent presque toujours un nom généré (lettres + suite de chiffres,
# ou mot générique + chiffres), jamais un vrai nom de marque. Une vraie startup
# early-stage a un nom de produit, pas "Lh-0831-35" ou "MediaSaver0316". Cette
# heuristique est un signal de MÉFIANCE supplémentaire, pas un rejet à elle
# seule (voir filter_scam_and_adult) : elle se combine toujours avec au moins
# un autre signal pour éviter de rejeter une vraie petite marque au nom insolite.
_GENERIC_PAGE_NAME_RE = re.compile(
    r"^[A-Za-z]{1,4}(-\d+){1,}$"     # "Lh-0831-35", "AB-1234"
    r"|^[A-Za-z]+\d{3,}$"            # "MediaSaver0316"
)


def is_generic_page_name(page_name: str) -> bool:
    return bool(_GENERIC_PAGE_NAME_RE.match(page_name.strip()))


def filter_scam_and_adult(grouped: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Exclut les arnaques reward/cashback et les apps de fiction interactive
    qui passaient entre les mailles de filter_out_of_scope. Combine mots-clés
    de contenu + heuristique de nom de page générique."""
    kept = {}
    for page_id, ads in grouped.items():
        page_name = ads[0].get("page_name", "")
        if matches_keywords(page_name, ads, SCAM_REWARD_KEYWORDS):
            print(f"  Exclu (pub reward/cashback suspecte) : {page_name}")
            continue
        if matches_keywords(page_name, ads, INTERACTIVE_FICTION_KEYWORDS):
            print(f"  Exclu (app de fiction interactive par chapitres) : {page_name}")
            continue
        if is_generic_page_name(page_name):
            print(f"  Exclu (nom de page générique/suspect) : {page_name}")
            continue
        kept[page_id] = ads
    return kept


# On ne veut cibler QUE des apps mobiles installables (Android et/ou iOS), pas
# des SaaS/sites web accessibles uniquement par navigateur. Pré-filtre rapide
# (couche 1) avant confirmation par Claude (couche 2, voir "is_mobile_app" dans
# classify_prospect) : si la pub ne mentionne aucun signal de téléchargement mobile,
# on exclut sans même appeler l'API Anthropic. Heuristique par mot-clé,
# imparfaite (une vraie app qui ne mentionne aucun de ces termes serait exclue
# à tort) mais en pratique les pubs d'apps mentionnent presque toujours un CTA
# de téléchargement explicite.
MOBILE_APP_KEYWORDS = [
    "app store", "google play", "play store", "download on the app store",
    "get it on google play", "available on ios", "available on android",
    "ios and android", "ios & android", "download the app", "download our app",
    "install the app", "scan to download", "ios app", "android app",
    "mobile app", "download now", "free download",
]


# Certaines pubs affirment explicitement NE PAS être une app ("no app needed",
# "sin apps", "sans application") - typiquement des objets connectés, stickers
# NFC/QR, plaques d'immatriculation intelligentes, etc. Si un de ces signaux
# négatifs apparaît, on exclut d'office, même si un mot-clé positif matchait
# par ailleurs (ex: une pub qui dit "no app, just scan the QR code").
NEGATIVE_MOBILE_APP_PHRASES = [
    "no app needed", "no app required", "without an app", "without any app",
    "sin apps", "sin app", "sans application", "sans app", "no download required",
    "no download needed", "don't need an app", "do not need an app",
]


# Certaines pubs mentionnent WhatsApp/Messenger/Telegram uniquement comme canal
# de contact ("Commandez sur WhatsApp", "DM us on Messenger"), sans aucun rapport
# avec une app mobile du prospect lui-même (souvent des commerces locaux ou du
# e-commerce classique). On retire ces mentions du texte avant de tester
# MOBILE_APP_KEYWORDS pour ne jamais les compter à tort comme un signal d'app.
CONTACT_CHANNEL_NOISE = ["whatsapp", "messenger", "telegram", "viber", "wa.me"]


def build_prospect_text(page_name: str, ads: list[dict]) -> str:
    text = page_name.lower()
    for ad in ads:
        text += " " + " ".join(ad.get("ad_creative_bodies", [])).lower()
        text += " " + " ".join(ad.get("ad_creative_link_titles", [])).lower()
    return text


def filter_non_english(grouped: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Ne garde que les pubs dont le texte est détecté comme anglais. Étape
    volontairement placée tôt (avant les autres filtres) : c'est un test local
    rapide (pas d'appel API), donc ça évite de dépenser du travail de filtrage
    et des appels Anthropic sur des pubs qu'on va de toute façon écarter.
    Fail-open : texte vide, détection ambiguë, ou paquet absent -> on garde
    plutôt que d'exclure sur un faux négatif technique."""
    if detect is None:
        print("  (langdetect non installé, filtre langue ignoré)")
        return grouped
    kept = {}
    for page_id, ads in grouped.items():
        page_name = ads[0].get("page_name", "")
        text = build_prospect_text(page_name, ads).strip()
        if not text:
            kept[page_id] = ads
            continue
        try:
            lang = detect(text)
        except LangDetectException:
            kept[page_id] = ads
            continue
        if lang != "en":
            print(f"  Exclu (langue détectée: {lang}) : {page_name}")
            continue
        kept[page_id] = ads
    return kept


def matches_keywords(page_name: str, ads: list[dict], keywords: list[str]) -> bool:
    text = build_prospect_text(page_name, ads)
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


def filter_non_mobile_apps(grouped: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Couche 1 du filtre app mobile : ne garde que les pages dont le texte de
    pub mentionne un signal de téléchargement Android/iOS (App Store, Google
    Play, "download the app"...). Les mentions WhatsApp/Messenger/Telegram
    (canal de contact, pas une app du prospect) sont retirées avant le test
    pour éviter les faux positifs. La couche 2 (confirmation par Claude) se
    fait ensuite dans classify_prospect, au moment de la classification."""
    kept = {}
    for page_id, ads in grouped.items():
        page_name = ads[0].get("page_name", "")
        text = build_prospect_text(page_name, ads)
        for noise in CONTACT_CHANNEL_NOISE:
            text = text.replace(noise, " ")
        if any(neg in text for neg in NEGATIVE_MOBILE_APP_PHRASES):
            print(f"  Exclu (annonce dit explicitement 'pas d'app') : {page_name}")
            continue
        if not any(keyword in text for keyword in MOBILE_APP_KEYWORDS):
            print(f"  Exclu (pas de signal app mobile) : {page_name}")
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
# Classification légère (API Anthropic) — plus de rédaction d'email
# ---------------------------------------------------------------------------

CLASSIFY_SYSTEM_PROMPT = """Tu qualifies des prospects repérés via une pub Meta \
(Facebook/Instagram) pour une agence de vidéos UGC ciblant des applications mobiles \
early-stage (pre-seed à seed). Pour chaque prospect fourni, réponds UNIQUEMENT avec \
un JSON de la forme :
{"is_mobile_app": true/false, "product_name": "...", "is_out_of_scope": true/false, "out_of_scope_reason": "..."}

"is_out_of_scope" vaut true dans les cas suivants (mets false sinon) :
- Contenu sexuel/suggestif explicite ou à connotation adulte (y compris les apps de \
fiction/roman/manga/webtoon "par chapitres" qui utilisent souvent des visuels suggestifs \
comme accroche, même si le texte de la pub reste vague sur le contenu réel)
- Pub de type "reward"/"cashback"/gain d'argent facile ("get $X on us", "claim your \
reward", "you won", carte cadeau, PayPal, etc.) : c'est un modèle de media-buying/arnaque \
classique, jamais une vraie app early-stage qui a besoin de UGC organique, même si l'app \
sous-jacente semble légitime (ex: un compteur de pas) - c'est le TYPE DE PUB qui disqualifie, \
pas le produit
- Toute pub qui ressemble à un schéma pyramidal, une arnaque, ou une promesse de gain \
irréaliste sans lien clair avec l'usage réel du produit

Si "is_out_of_scope" est true, remplis "out_of_scope_reason" avec une courte explication \
(quelques mots). Sinon laisse-le vide ("").

"is_mobile_app" vaut true UNIQUEMENT si le texte de la pub indique clairement qu'il \
s'agit d'une application mobile installable sur smartphone (Android et/ou iOS) : \
mention d'un téléchargement, de l'App Store, de Google Play, d'un usage "sur votre \
téléphone", etc. Mets false si c'est un site web, un SaaS accessible uniquement par \
navigateur/desktop, ou si le texte ne permet pas de conclure avec certitude qu'il \
s'agit d'une app mobile. En cas de doute réel, mets false plutôt que d'inventer une \
certitude que le texte ne donne pas.

ATTENTION - piège fréquent : la mention de WhatsApp, Messenger, Telegram ou Viber \
comme canal de contact ("Commandez sur WhatsApp", "DM us on Messenger") NE COMPTE \
JAMAIS comme preuve d'une app mobile. Ce sont des outils de messagerie tiers utilisés \
par n'importe quel commerce (restaurant, boutique...) pour prendre des commandes, ça \
n'a aucun rapport avec le produit du prospect lui-même. Ignore complètement ces \
mentions et base ta décision uniquement sur le reste du texte de la pub.

ATTENTION - deuxième piège : si la pub affirme explicitement qu'AUCUNE app n'est \
nécessaire ("no app needed", "sin apps", "sans application" - souvent un objet \
connecté, sticker NFC/QR, dispositif physique), mets is_mobile_app à false sans \
hésiter, même si d'autres mots du texte évoquent la tech ou le mobile.

"product_name" est le nom du produit/app tel qu'il apparaît dans le texte de la pub \
(pas le nom de la page Facebook, qui peut être un nom de personne si la pub tourne \
depuis un profil perso). Cherche un nom de marque/produit explicite dans le texte de \
la pub fourni. Si aucun nom de produit clair n'apparaît dans le texte, mets null pour \
ce champ plutôt que d'inventer un nom.

Pas de texte avant ou après le JSON, pas de balises markdown."""


def classify_prospect(page_name: str, ad_bodies: list[str]) -> dict | None:
    """Appel Anthropic léger : confirme si c'est une app mobile (couche 2 du
    filtre) et extrait le nom du produit. Ne rédige plus d'email."""
    if not ANTHROPIC_API_KEY:
        return None

    ad_text = "\n---\n".join(b for b in ad_bodies if b) or "(texte de pub non disponible)"
    user_prompt = f"Prospect : {page_name}\n\nTexte(s) de leur pub Meta actuelle :\n{ad_text}"

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
                "max_tokens": 200,
                "thinking": {"type": "disabled"},
                "system": CLASSIFY_SYSTEM_PROMPT,
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
            print(f"Réponse Anthropic vide pour '{page_name}'. stop_reason={data.get('stop_reason')}", file=sys.stderr)
            return None
        return json.loads(text)
    except Exception as e:  # noqa: BLE001 - on ne veut pas planter tout le run pour une classification ratée
        print(f"Erreur classification pour '{page_name}': {e}", file=sys.stderr)
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

    # Un seul message texte simple par prospect (pas d'embed) : Nom / Ad
    for page_id, ads, classification, media_type, website, contact_email in prospects:
        page_name = ads[0].get("page_name", "?")
        snapshot = ads[0].get("ad_snapshot_url", "")
        display_name = (classification.get("product_name") if classification else None) or page_name

        content = f"Nom: {display_name}\nAd: {snapshot or 'N/A'}"
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
        classification = classify_prospect(page_name, ad_bodies)
        demo_prospects = [("demo-0000", DEMO_PROSPECT_ADS, classification, media_type, None, None)]
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

    grouped = filter_non_english(grouped)
    print(f"Après filtre langue (anglais uniquement): {len(grouped)}")

    grouped = filter_out_of_scope(grouped)
    print(f"Après exclusion finance/hors périmètre: {len(grouped)}")

    grouped = filter_scam_and_adult(grouped)
    print(f"Après exclusion arnaques reward/contenu adulte (couche 1, mots-clés): {len(grouped)}")

    grouped = filter_non_mobile_apps(grouped)
    print(f"Après filtre app mobile (App Store/Google Play, couche 1): {len(grouped)}")

    small = filter_small_advertisers(grouped)
    print(f"Après filtre 'petit compte' (<= {MAX_ACTIVE_ADS_PER_PAGE} pubs): {len(small)}")

    new_prospects_raw = [(pid, ads) for pid, ads in small.items() if pid not in seen]
    print(f"Nouveaux (jamais signalés): {len(new_prospects_raw)}")

    if len(new_prospects_raw) > MAX_PROSPECTS_PER_RUN:
        print(f"  -> plafonné à {MAX_PROSPECTS_PER_RUN} pour ce run, le reste sera traité au prochain passage")
        new_prospects_raw = new_prospects_raw[:MAX_PROSPECTS_PER_RUN]

    new_prospects = []
    excluded_not_mobile = []
    for page_id, ads in new_prospects_raw:
        page_name = ads[0].get("page_name", "?")
        ad_bodies = [b for ad in ads for b in ad.get("ad_creative_bodies", [])]
        media_type = dominant_media_type(ads)

        print(f"  Classification de '{page_name}' ({media_type})...")
        classification = classify_prospect(page_name, ad_bodies)

        # Couche 2 : Claude confirme/infirme que c'est bien une app mobile.
        # On ne rejette que sur un "false" explicite - si l'appel API a raté
        # (classification=None) ou n'a pas donné cette info, on garde le
        # prospect par défaut plutôt que de le perdre sur un simple souci
        # technique.
        if classification and classification.get("is_mobile_app") is False:
            print(f"  Exclu (Claude: pas une app mobile) : {page_name}")
            excluded_not_mobile.append(page_id)
            continue

        if classification and classification.get("is_out_of_scope") is True:
            reason = classification.get("out_of_scope_reason") or "non précisée"
            print(f"  Exclu (Claude: hors périmètre - {reason}) : {page_name}")
            excluded_not_mobile.append(page_id)
            continue

        new_prospects.append((page_id, ads, classification, media_type, None, None))

    post_to_discord(new_prospects)

    # Les exclus couche 2 sont aussi marqués "seen" : sans ça, ils reviendraient
    # au run suivant et on repaierait un appel Anthropic pour re-confirmer la
    # même exclusion indéfiniment.
    seen.update(pid for pid, _, _, _, _, _ in new_prospects)
    seen.update(excluded_not_mobile)
    save_seen(seen)


if __name__ == "__main__":
    main()
