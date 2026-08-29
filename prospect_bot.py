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

# Mots-clés à surveiller. Ajuste selon vos ICP (SaaS, app, startup, etc.)
SEARCH_TERMS = ["app", "SaaS", "startup"]

# Pays UE (là où l'API donne des données commerciales fiables)
AD_REACHED_COUNTRIES = [
    "FR", "DE", "ES", "IT", "NL", "BE", "PT", "IE", "AT",
    "PL", "SE", "DK", "FI", "LU",
]

# Un annonceur avec plus de X pubs actives sur la période = probablement
# une boite qui a déjà une équipe growth/ads => on l'exclut.
MAX_ACTIVE_ADS_PER_PAGE = 5

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

    while url:
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

        next_url = data.get("paging", {}).get("next")
        url = next_url
        use_params = None  # l'URL "next" contient déjà tous les params

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


def is_finance_related(page_name: str, ads: list[dict]) -> bool:
    """Exclut les apps finance/banque/crypto/investissement de la prospection."""
    text = page_name.lower()
    for ad in ads:
        text += " " + " ".join(ad.get("ad_creative_bodies", [])).lower()
        text += " " + " ".join(ad.get("ad_creative_link_titles", [])).lower()
    return any(keyword in text for keyword in FINANCE_KEYWORDS)


def filter_out_finance(grouped: dict[str, list[dict]]) -> dict[str, list[dict]]:
    kept = {}
    for page_id, ads in grouped.items():
        page_name = ads[0].get("page_name", "")
        if is_finance_related(page_name, ads):
            print(f"  Exclu (finance) : {page_name}")
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
# Site web du prospect + email de contact
# ---------------------------------------------------------------------------

DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", re.I)
MAILTO_RE = re.compile(r"mailto:([^\"'?\s]+)", re.I)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# Domaines/motifs à ignorer : ce sont des adresses techniques, pas des contacts utiles
EMAIL_BLOCKLIST_SUBSTRINGS = [
    "example.com", "sentry.io", "wixpress.com", "godaddy", "yourdomain",
    "domain.com", ".png", ".jpg", ".svg", "noreply", "no-reply",
]


def guess_website(ads: list[dict]) -> str | None:
    """Meta ne donne pas d'URL de destination directe, mais le 'caption' du lien
    affiche en général le domaine du site (ex: 'getloop.app'). Heuristique, pas
    garantie à 100%."""
    for ad in ads:
        for caption in ad.get("ad_creative_link_captions", []):
            candidate = caption.strip().lower().removeprefix("www.")
            if DOMAIN_RE.match(candidate):
                return f"https://{candidate}"
    return None


def find_email_on_website(url: str) -> str | None:
    """Va chercher un email de contact sur la page d'accueil du site (et /contact
    si rien trouvé). Best-effort : si le site bloque, timeout, ou ne liste pas
    d'email publiquement, on renvoie simplement None."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; TantoLabBot/1.0)"}

    for path in ("", "contact", "contact-us"):
        try:
            resp = requests.get(url.rstrip("/") + "/" + path, headers=headers, timeout=10)
            if resp.status_code != 200:
                continue
            html = resp.text

            candidates = MAILTO_RE.findall(html) or EMAIL_RE.findall(html)
            for candidate in candidates:
                candidate = candidate.strip().lower()
                if any(bad in candidate for bad in EMAIL_BLOCKLIST_SUBSTRINGS):
                    continue
                return candidate
        except requests.RequestException:
            continue

    return None


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

Structure obligatoire en 8 blocs (contenu en français ici, sortie en anglais) — CHAQUE \
bloc numéroté ci-dessous correspond à UN paragraphe séparé par un saut de ligne double \
dans le JSON final, jamais fusionné avec le bloc suivant :
0. Une courte formule d'introduction avant l'accroche (ex : "Hey," ou "Hi,"), jamais \
"Dear Sir/Madam" ni de formalisme excessif. Son propre bloc, séparé de l'accroche.
1. Accroche : la première vraie phrase capte l'attention en ciblant un problème précis \
et concret ancré dans LEUR pub actuelle (pas une question générique de type "want to \
double your sales?").
2. Problème concret : nomme le vrai problème que révèle leur créa actuelle. Le type de \
créa dominant (IMAGE, MEME ou MIX) t'est donné en amont du texte de la pub, adapte \
l'angle du problème en conséquence, ne réutilise jamais le même angle pour les trois :
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
7. Appel à l'action + signature : une seule action claire et précise qui pousse à \
répondre à cet email pour dire s'ils veulent tenter l'offre, jamais à proposer un call \
ou un rendez-vous, jamais vague ("feel free to reach out"). La signature ("Thomas, \
Tanto Lab") suit, dans le même bloc ou son propre bloc.

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
- Pour le CTA (point 4) : INTERDIT de proposer un call, un rendez-vous ou un créneau, \
sous quelque forme que ce soit ("quick call", "jump on a call", "worth a chat", "hop on \
a 10-min call"...). Le CTA pousse uniquement à répondre à l'email. Varie la formulation \
à chaque email pour ne jamais répéter le même patron : par exemple demander si un \
format donné les intéresse, proposer d'envoyer 2-3 exemples similaires en réponse, ou \
demander une info précise sur leur projet. Une phrase, jamais plus.
- Court : 110-150 mots pour le corps (intro incluse)
- Format obligatoire du champ "body" : chacun des 8 blocs de la structure ci-dessus \
(0, 1, 2, 3, 4, 5, 6, 7) est son propre paragraphe, séparé du suivant par un saut de \
ligne double ("\\n\\n" dans le JSON), jamais fusionné avec un autre, jamais un pavé de \
texte compact. Chaque bloc reste court (1-2 phrases), jamais un paragraphe de 3+ \
phrases.

Rappel : "subject" et "body" doivent être rédigés entièrement en anglais. Le champ \
"problem" ci-dessous, lui, doit être écrit en FRANÇAIS (usage interne pour l'équipe).

Réponds UNIQUEMENT avec un JSON de la forme :
{"problem": "...", "subject": "...", "body": "..."}
où "problem" est UNE seule phrase courte en français qui résume le problème créa \
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


MEDIA_TYPE_LABELS = {
    "IMAGE": "image statique",
    "MEME": "meme/template",
    "MIX": "image + meme",
}


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

    # Un message (embed) par prospect, avec l'email prêt à copier-coller
    for page_id, ads, email, media_type, website, contact_email in prospects:
        page_name = ads[0].get("page_name", "?")
        snapshot = ads[0].get("ad_snapshot_url", "")
        media_label = MEDIA_TYPE_LABELS.get(media_type, media_type)

        fields = [
            {"name": "Pubs actives", "value": f"{len(ads)} ({media_label})", "inline": True},
            {"name": "Ad Library", "value": f"[Voir la pub]({snapshot})" if snapshot else "N/A", "inline": True},
        ]

        if contact_email:
            fields.append({"name": "✅ Email trouvé", "value": contact_email, "inline": False})
        elif website:
            fields.append({"name": "🌐 Site web", "value": website, "inline": False})
        else:
            fields.append({"name": "🌐 Site web", "value": "Non trouvé", "inline": False})

        if email:
            fields.append({"name": "🔎 Problème détecté", "value": email.get("problem", "?")[:1024], "inline": False})
            fields.append({"name": "📧 Subject", "value": email.get("subject", "?")[:1024], "inline": False})
            fields.append({"name": "📧 Body", "value": email.get("body", "?")[:1024], "inline": False})
        else:
            fields.append({"name": "📧 Email", "value": "Non généré (ANTHROPIC_API_KEY manquant ou erreur, voir logs).", "inline": False})

        embed = {
            "title": page_name,
            "color": 0x5865F2,
            "fields": fields,
        }
        requests.post(DISCORD_WEBHOOK_URL, json={"username": BOT_NAME, "embeds": [embed]}, timeout=15)
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
        website = guess_website(DEMO_PROSPECT_ADS)
        email = draft_email(page_name, ad_bodies, media_type)
        demo_prospects = [("demo-0000", DEMO_PROSPECT_ADS, email, media_type, website, None)]
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

    grouped = filter_out_finance(grouped)
    print(f"Après exclusion finance/banque/crypto: {len(grouped)}")

    small = filter_small_advertisers(grouped)
    print(f"Après filtre 'petit compte' (<= {MAX_ACTIVE_ADS_PER_PAGE} pubs): {len(small)}")

    new_prospects_raw = [(pid, ads) for pid, ads in small.items() if pid not in seen]
    print(f"Nouveaux (jamais signalés): {len(new_prospects_raw)}")

    new_prospects = []
    for page_id, ads in new_prospects_raw:
        page_name = ads[0].get("page_name", "?")
        ad_bodies = [b for ad in ads for b in ad.get("ad_creative_bodies", [])]
        media_type = dominant_media_type(ads)

        website = guess_website(ads)
        contact_email = None
        if website:
            print(f"  Site trouvé pour '{page_name}': {website}, recherche d'email...")
            contact_email = find_email_on_website(website)
            print(f"  -> email {'trouvé' if contact_email else 'non trouvé'}")

        print(f"  Rédaction email pour '{page_name}' ({media_type})...")
        email = draft_email(page_name, ad_bodies, media_type)
        new_prospects.append((page_id, ads, email, media_type, website, contact_email))

    post_to_discord(new_prospects)

    seen.update(pid for pid, _, _, _, _, _ in new_prospects)
    save_seen(seen)


if __name__ == "__main__":
    main()
