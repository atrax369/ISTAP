"""
İşTap — Robust Scraper (Gemini AI + Supabase)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Dizayndan TAMAMILƏ ASILI DEYİL.
BeautifulSoup yalnız xam mətn çıxarır → Gemini strukturlaşdırır.

Environment variables (GitHub Secrets / .env):
    GEMINI_API_KEY
    SUPABASE_URL
    SUPABASE_KEY
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import google.generativeai as genai
import requests
from bs4 import BeautifulSoup
from supabase import Client, create_client

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ishtap")

# ── Environment variables ─────────────────────────────────────────────────────
GEMINI_API_KEY: str = os.environ["GEMINI_API_KEY"]
SUPABASE_URL:   str = os.environ["SUPABASE_URL"]
SUPABASE_KEY:   str = os.environ["SUPABASE_KEY"]

# ── İstemci başlatma ─────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
gemini = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    generation_config=genai.GenerationConfig(
        temperature=0.0,          # Deterministik çıxış
        response_mime_type="application/json",
    ),
)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Sabit parametrlər ─────────────────────────────────────────────────────────
TARGET_URL   = "https://www.hellojob.az/vakansiyalar"
TABLE_NAME   = "jobs"
CLEANUP_DAYS = 45
MAX_CHARS    = 28_000   # Gemini 1.5-flash kontekst limiti üçün ehtiyatlı dəyər
HEADERS      = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# ── Skill lüğəti ─────────────────────────────────────────────────────────────
SKILL_KEYWORDS: dict[str, list[str]] = {
    "fiziki_texniki": [
        "fəhlə", "ağır yük", "dözümlülük", "usta", "təmir",
        "qaynaq", "montaj", "elektrik", "tikinti",
        "mühafizə", "təmizlik", "anbar",
    ],
    "suruculuk_kuryer": [
        "sürücü", "b kateqoriyası", "bc kateqoriyası", "bc",
        "moped", "kuryer", "naviqasiya", "ekspeditor", "moto-kuryer",
    ],
    "xidmet_satis": [
        "ofisiant", "kassa", "kassir", "satış", "müştəri xidməti",
        "barista", "admin", "resepsion", "barmen", "məsləhətçi",
    ],
    "ofis_idareetme": [
        "excel", "microsoft office", "1c", "mühasibat",
        "kargüzarlıq", "maliyyə", "kadr", "analitik",
    ],
    "reqemsal_it": [
        "sql", "python", "smm", "dizayn", "proqramçı", "it dəstək",
    ],
}

GEMINI_SYSTEM_PROMPT = """
Sən iş elanları analizatoru botsan.
Sənə hellojob.az saytının bir səhifəsinin xam mətni veriləcək.

Vəzifən:
1. Bu mətndən bütün aktiv iş vakansiyalarını tap.
2. Hər elan üçün aşağıdakı JSON strukturunu doldur:
   {
     "title":       "Vakansiya adı (tam, olduğu kimi)",
     "company":     "Şirkətin adı",
     "source_link": "Əgər mətndə elanın özünə aid xüsusi URL və ya ID varsa
                     'https://www.hellojob.az/vacancy/ID' formatında tam URL yaz.
                     Əgər yoxdursa 'https://www.hellojob.az/vakansiyalar' qoy."
   }
3. Cavab olaraq YALNIZ təmiz JSON massivi ([...]) qaytar.
   Markdown blokları (```json), izahat, giriş sözü yazmaq QADAĞANDIR.
   Əgər heç bir vakansiya tapılmasa, boş massiv [] qaytar.
""".strip()


# ═════════════════════════════════════════════════════════════════════════════
#  1. XAM MƏTNİ ÇƏKMƏ (dizayndan asılı deyil)
# ═════════════════════════════════════════════════════════════════════════════
def fetch_raw_text(url: str) -> str:
    """
    Hədəf URL-dən tam HTML çəkir, BeautifulSoup ilə
    YALNIZ xam mətni qaytarır. Heç bir CSS klasa toxunmur.
    """
    log.info("HTML çəkilir: %s", url)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=25)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Səhifə açılmadı: %s", exc)
        raise

    soup = BeautifulSoup(resp.text, "html.parser")

    # Skript, stil və gizli elementləri sil (Gemini üçün küy azalır)
    for tag in soup(["script", "style", "noscript", "meta", "head"]):
        tag.decompose()

    raw_text = soup.get_text(separator=" ", strip=True)

    # Çox uzun mətnləri Gemini limitinə görə kəs
    if len(raw_text) > MAX_CHARS:
        log.warning(
            "Mətn çox uzundur (%d simvol), %d simvola kəsildi.",
            len(raw_text), MAX_CHARS,
        )
        raw_text = raw_text[:MAX_CHARS]

    log.info("Xam mətn hazır: %d simvol.", len(raw_text))
    return raw_text


# ═════════════════════════════════════════════════════════════════════════════
#  2. GEMİNİ İLƏ STRUKTURLAŞDIRMA
# ═════════════════════════════════════════════════════════════════════════════
def parse_with_gemini(raw_text: str, retry: int = 2) -> list[dict]:
    """
    Xam mətni Gemini-yə göndərir, JSON massiv alır.
    Şəbəkə/API xətasında `retry` dəfə yenidən cəhd edir.
    """
    prompt = (
        f"{GEMINI_SYSTEM_PROMPT}\n\n"
        f"---MƏTN BAŞLADI---\n{raw_text}\n---MƏTN BİTDİ---"
    )

    for attempt in range(1, retry + 2):
        try:
            log.info("Gemini-yə sorğu göndərilir (cəhd %d)...", attempt)
            response = gemini.generate_content(prompt)
            raw_json = response.text.strip()

            # Gemini bəzən ```json ... ``` bloku qaytara bilər — ehtiyatlı yanaş
            if raw_json.startswith("```"):
                raw_json = raw_json.split("```")[1]
                if raw_json.lower().startswith("json"):
                    raw_json = raw_json[4:]
                raw_json = raw_json.strip()

            vacancies: list[dict] = json.loads(raw_json)
            log.info("Gemini %d vakansiya qaytardı.", len(vacancies))
            return vacancies

        except json.JSONDecodeError as exc:
            log.error("JSON parse xətası (cəhd %d): %s", attempt, exc)
            log.debug("Gemini cavabı: %s", response.text[:500])
        except Exception as exc:
            log.error("Gemini API xətası (cəhd %d): %s", attempt, exc)

        if attempt <= retry:
            wait = 5 * attempt
            log.info("%d saniyə gözlənilir...", wait)
            time.sleep(wait)

    log.error("Gemini-dən etibarlı cavab alınmadı. Boş siyahı qaytarılır.")
    return []


# ═════════════════════════════════════════════════════════════════════════════
#  3. YERLİ SKİLL ANALİZİ
# ═════════════════════════════════════════════════════════════════════════════
def extract_skills(title: str, company: str) -> list[str]:
    """
    AI-dan gələn title + company mətnini lokal lüğətlə yoxlayır
    və uyğun skill-ləri qaytarır (case-insensitive).
    """
    combined = f"{title} {company}".lower()
    found: set[str] = set()

    for keywords in SKILL_KEYWORDS.values():
        for kw in keywords:
            if kw.lower() in combined:
                found.add(kw)

    return sorted(found)


# ═════════════════════════════════════════════════════════════════════════════
#  4. KÖHNƏ ELANLAR TƏMİZLƏNMƏSİ (45 gün)
# ═════════════════════════════════════════════════════════════════════════════
def cleanup_old_jobs() -> None:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=CLEANUP_DAYS)
    ).isoformat()
    log.info("Köhnə elanlar silinir (cutoff: %s)...", cutoff)

    try:
        resp = (
            supabase.table(TABLE_NAME)
            .delete()
            .lt("posted_at", cutoff)
            .execute()
        )
        deleted = len(resp.data) if resp.data else 0
        log.info("%d köhnə elan silindi.", deleted)
    except Exception as exc:
        log.error("Təmizlik zamanı xəta: %s", exc)


# ═════════════════════════════════════════════════════════════════════════════
#  5. SUPABASE UPSERT
# ═════════════════════════════════════════════════════════════════════════════
def save_to_supabase(vacancies: list[dict]) -> None:
    """
    Elanları `jobs` cədvəlinə UPSERT edir.
    Konflikt şərti: (title, company) composite unique key.
    Mövcuddursa → posted_at yenilənir, yoxdursa → yeni sıra əlavə edilir.

    Supabase SQL-də tələb olunan constraint:
        ALTER TABLE jobs
        ADD CONSTRAINT jobs_title_company_unique UNIQUE (title, company);
    """
    if not vacancies:
        log.info("Yazılacaq elan yoxdur.")
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    records = []

    for v in vacancies:
        title   = (v.get("title")       or "").strip()
        company = (v.get("company")     or "Naməlum").strip()
        link    = (v.get("source_link") or TARGET_URL).strip()

        if not title:
            log.debug("Başlıqsız elan atlandı: %s", v)
            continue

        records.append({
            "title":       title,
            "company":     company,
            "source_link": link,
            "skills":      extract_skills(title, company),
            "posted_at":   now_iso,
        })

    log.info("%d elan Supabase-ə göndərilir...", len(records))

    try:
        resp = (
            supabase.table(TABLE_NAME)
            .upsert(
                records,
                on_conflict="title,company",
                ignore_duplicates=False,   # mövcuddursa posted_at yenilə
            )
            .execute()
        )
        upserted = len(resp.data) if resp.data else 0
        log.info("%d elan upsert edildi.", upserted)
    except Exception as exc:
        log.error("Supabase upsert xətası: %s", exc)
        raise


# ═════════════════════════════════════════════════════════════════════════════
#  6. ANA PROSES
# ═════════════════════════════════════════════════════════════════════════════
def main() -> None:
    log.info("══════════ İşTap Robust Scraper Başladı ══════════")

    # Addım 1 — Köhnə elanları sil
    cleanup_old_jobs()

    # Addım 2 — Xam mətni çək (CSS-dən asılı deyil)
    raw_text = fetch_raw_text(TARGET_URL)

    # Addım 3 — Gemini ilə strukturlaşdır
    vacancies = parse_with_gemini(raw_text)

    # Addım 4 — Lokal skill analizi + bazaya yaz
    save_to_supabase(vacancies)

    log.info("══════════ İşTap Robust Scraper Tamamlandı ══════════")


if __name__ == "__main__":
    main()
