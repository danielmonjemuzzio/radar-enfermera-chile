import os
import re
import sys
import html
import json
import time
import random
import hashlib
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Set
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==========================================
# CONFIGURACION GENERAL Y LOGGING
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("RadarEnfermeraCL")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

SEEN_JOBS_FILE = "seen_jobs.json"
MAX_JOBS_PER_RUN = 30
MAX_SEEN_HISTORY = 2500

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
]

MUST_INCLUDE_REGEX = re.compile(
    r"\b(enfermer[ao]s?|enfermer[ií]a|eu\b|e\.u\.)\b", re.IGNORECASE
)

EXCLUDE_REGEX = re.compile(
    r"\b(t[eé]cnico\s+en\s+enfermer[ií]a|tens\b|auxiliar\s+de\s+enfermer[ií]a|"
    r"veterinari[ao]|agr[oó]nom[ao]|prevencionista|guardia|aseo|limpieza)\b",
    re.IGNORECASE,
)


def create_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        read=3,
        connect=3,
        backoff_factor=1.2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def get_headers() -> Dict[str, str]:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    }


def load_seen_jobs() -> Set[str]:
    if not os.path.exists(SEEN_JOBS_FILE):
        return set()
    try:
        with open(SEEN_JOBS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data if isinstance(data, list) else [])
    except Exception as e:
        logger.warning(f"No se pudo leer {SEEN_JOBS_FILE}: {e}")
        return set()


def save_seen_jobs(seen: Set[str]) -> None:
    seen_list = list(seen)[-MAX_SEEN_HISTORY:]
    try:
        with open(SEEN_JOBS_FILE, "w", encoding="utf-8") as f:
            json.dump(seen_list, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error guardando {SEEN_JOBS_FILE}: {e}")


def make_job_id(title: str, company: str, url: str) -> str:
    clean_url = url.split("?")[0].strip().lower()
    raw = f"{title.strip().lower()}|{company.strip().lower()}|{clean_url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def is_valid_nurse_offer(title: str, description: str = "") -> bool:
    if not MUST_INCLUDE_REGEX.search(title):
        return False
    if EXCLUDE_REGEX.search(title) and not re.search(
        r"universitari[ao]|profesional", title, re.IGNORECASE
    ):
        return False
    return True


def score_job(job: Dict) -> int:
    score = 50
    text = f"{job.get('title', '')} {job.get('description', '')} {job.get('company', '')}".lower()

    if any(
        k in text
        for k in [
            "hospital",
            "servicio de salud",
            "cesfam",
            "clínica",
            "clinica",
            "redsalud",
            "uc christus",
            "bupa",
            "indisa",
            "alemana",
            "dávila",
            "davila",
            "achs",
            "mutual",
            "ist",
        ]
    ):
        score += 20

    if any(k in text for k in ["grado", "eus", "$", "sueldo", "renta", "planta", "contrata", "indefinido"]):
        score += 15

    if any(
        k in text
        for k in [
            "uci",
            "uti",
            "urgencia",
            "pabellón",
            "pabellon",
            "diálisis",
            "dialisis",
            "oncología",
            "neonatología",
            "coordinador",
            "jefatura",
            "gestión",
        ]
    ):
        score += 10

    fresh = job.get("date_label", "").lower()
    if any(k in fresh for k in ["hoy", "hora", "minuto", "recién", "ayer", "1 día"]):
        score += 15

    return score


def scrape_computrabajo(session: requests.Session) -> List[Dict]:
    jobs = []
    urls = [
        "https://cl.computrabajo.com/trabajo-de-enfermera?pubdate=3",
        "https://cl.computrabajo.com/trabajo-de-enfermero?pubdate=3",
        "https://cl.computrabajo.com/trabajo-de-enfermeria?pubdate=3",
    ]
    for url in urls:
        try:
            resp = session.get(url, headers=get_headers(), timeout=15)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "lxml")
            articles = soup.select("article.box_offer")
            for art in articles:
                a_tag = art.select_one("h2 a, a.js-o-link")
                if not a_tag:
                    continue
                title = a_tag.get_text(strip=True)
                href = a_tag.get("href", "")
                link = urljoin("https://cl.computrabajo.com", href)

                comp_tag = art.select_one("p.fs16, a.fc_base")
                company = comp_tag.get_text(strip=True) if comp_tag else "Computrabajo"

                loc_tag = art.select_one("p.fs13")
                location = loc_tag.get_text(" ", strip=True) if loc_tag else "Chile"

                desc_tag = art.select_one("p.mb10")
                desc = desc_tag.get_text(" ", strip=True) if desc_tag else ""

                if is_valid_nurse_offer(title, desc):
                    jobs.append(
                        {
                            "title": title,
                            "company": company,
                            "location": location,
                            "url": link,
                            "portal": "Computrabajo CL",
                            "date_label": "Últimas 48h",
                            "description": desc,
                        }
                    )
            time.sleep(random.uniform(1.0, 2.0))
        except Exception as e:
            logger.warning(f"Error en Computrabajo: {e}")
    logger.info(f"Computrabajo CL: {len(jobs)} ofertas.")
    return jobs


def scrape_chiletrabajos(session: requests.Session) -> List[Dict]:
    jobs = []
    urls = [
        "https://www.chiletrabajos.cl/encuentra-un-empleo?2=enfermera&13=&f=2",
        "https://www.chiletrabajos.cl/encuentra-un-empleo?2=enfermero&13=&f=2",
    ]
    for url in urls:
        try:
            resp = session.get(url, headers=get_headers(), timeout=15)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "lxml")
            items = soup.select("div.job-item")
            for item in items:
                title_tag = item.select_one("h2.title a")
                if not title_tag:
                    continue
                title = title_tag.get_text(strip=True)
                link = urljoin("https://www.chiletrabajos.cl", title_tag.get("href", ""))

                meta_tags = item.select("h3.meta")
                company_loc = meta_tags[0].get_text(" ", strip=True) if meta_tags else "Chile"
                parts = [p.strip() for p in company_loc.split(",") if p.strip()]
                company = parts[0] if parts else "Confidencial"
                location = parts[-1] if len(parts) > 1 else "Chile"

                desc_tag = item.select_one("p.description")
                desc = desc_tag.get_text(" ", strip=True) if desc_tag else ""

                if is_valid_nurse_offer(title, desc):
                    jobs.append(
                        {
                            "title": title,
                            "company": company,
                            "location": location,
                            "url": link,
                            "portal": "Chiletrabajos",
                            "date_label": "Reciente (48h)",
                            "description": desc,
                        }
                    )
            time.sleep(random.uniform(1.0, 2.0))
        except Exception as e:
            logger.warning(f"Error en Chiletrabajos: {e}")
    logger.info(f"Chiletrabajos: {len(jobs)} ofertas.")
    return jobs


def scrape_jora_chile(session: requests.Session) -> List[Dict]:
    jobs = []
    for q in ["Enfermera", "Enfermera Clinica"]:
        url = f"https://cl.jora.com/j?q={quote_plus(q)}&l=Chile&a=2d&sort=date"
        try:
            resp = session.get(url, headers=get_headers(), timeout=15)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "lxml")
            cards = soup.select("div.job-card, div.result")
            for card in cards:
                a_tag = card.select_one("a.job-link, h2 a, h3 a")
                if not a_tag:
                    continue
                title = a_tag.get_text(strip=True)
                link = urljoin("https://cl.jora.com", a_tag.get("href", ""))
                comp_tag = card.select_one("span.job-company, div.company")
                loc_tag = card.select_one("a.job-location, span.job-location, div.location")
                desc_tag = card.select_one("div.job-abstract, div.summary")

                company = comp_tag.get_text(strip=True) if comp_tag else "Jora Chile"
                location = loc_tag.get_text(strip=True) if loc_tag else "Chile"
                desc = desc_tag.get_text(" ", strip=True) if desc_tag else ""

                if is_valid_nurse_offer(title, desc):
                    jobs.append(
                        {
                            "title": title,
                            "company": company,
                            "location": location,
                            "url": link,
                            "portal": "Jora Chile",
                            "date_label": "Últimas 48h",
                            "description": desc,
                        }
                    )
            time.sleep(random.uniform(1.0, 2.0))
        except Exception as e:
            logger.warning(f"Error en Jora Chile: {e}")
    logger.info(f"Jora Chile: {len(jobs)} ofertas.")
    return jobs


def scrape_multiportal_rss(session: requests.Session) -> List[Dict]:
    jobs = []
    portal_groups = [
        ("Empleos Públicos / BNE", '(site:empleospublicos.cl OR site:bne.cl) ("enfermera" OR "enfermero") when:2d'),
        ("Laborum / Trabajando", '(site:laborum.cl OR site:trabajando.cl) ("enfermera" OR "enfermero") when:2d'),
        ("LinkedIn / Indeed", '(site:cl.linkedin.com/jobs OR site:cl.indeed.com) ("enfermera" OR "enfermero") when:2d'),
        ("Clínicas", '(site:redsalud.cl OR site:bupa.cl OR site:ucchristus.cl OR site:indisa.cl OR site:alemana.cl) ("enfermera" OR "enfermero") when:2d'),
    ]

    for portal_label, dork in portal_groups:
        rss_url = f"https://news.google.com/rss/search?q={quote_plus(dork)}&hl=es-419&gl=CL&ceid=CL:es-419"
        try:
            resp = session.get(rss_url, headers=get_headers(), timeout=15)
            if resp.status_code == 200 and resp.content:
                root = ET.fromstring(resp.content)
                for item in root.findall(".//item"):
                    raw_title = item.findtext("title") or ""
                    link = item.findtext("link") or ""
                    pub_date = item.findtext("pubDate") or "Últimas 48h"
                    source = item.findtext("source") or portal_label

                    title_parts = raw_title.rsplit(" - ", 1)
                    title = title_parts[0].strip()
                    company = title_parts[1].strip() if len(title_parts) > 1 else source

                    if is_valid_nurse_offer(title):
                        jobs.append(
                            {
                                "title": title,
                                "company": company,
                                "location": "Chile",
                                "url": link,
                                "portal": source,
                                "date_label": pub_date[:16],
                                "description": raw_title,
                            }
                        )
        except Exception as e:
            logger.warning(f"Error RSS ({portal_label}): {e}")

        dork_bing = dork.replace("when:2d", "").strip()
        bing_url = f"https://www.bing.com/search?q={quote_plus(dork_bing)}&format=rss"
        try:
            resp_b = session.get(bing_url, headers=get_headers(), timeout=15)
            if resp_b.status_code == 200 and b"<rss" in resp_b.content[:250]:
                root_b = ET.fromstring(resp_b.content)
                for item in root_b.findall(".//item"):
                    title = (item.findtext("title") or "").strip()
                    link = (item.findtext("link") or "").strip()
                    desc = (item.findtext("description") or "").strip()
                    if is_valid_nurse_offer(title, desc):
                        jobs.append(
                            {
                                "title": title,
                                "company": portal_label,
                                "location": "Chile",
                                "url": link,
                                "portal": portal_label,
                                "date_label": "Últimas 48h",
                                "description": desc,
                            }
                        )
        except Exception as e:
            logger.debug(f"Bing RSS ({portal_label}): {e}")

        time.sleep(random.uniform(0.8, 1.2))
    logger.info(f"Multiportal RSS: {len(jobs)} ofertas.")
    return jobs


def analyze_nurse_offer(job: Dict, session: requests.Session) -> str:
    text = f"{job['title']} | {job['company']} | {job['location']} | {job.get('description', '')}"

    turno = "Por confirmar en entrevista (Diurno / Turnos)"
    if re.search(r"4to\s*turno|cuarto\s*turno", text, re.I):
        turno = "Cuarto Turno / 4° Turno"
    elif re.search(r"diurno|lunes\s+a\s+viernes", text, re.I):
        turno = "Horario Diurno (Lunes a Viernes)"

    requisitos = ["Registro SIS", "Título Enfermera/o"]
    if re.search(r"iaas", text, re.I):
        requisitos.append("Curso IAAS")
    if re.search(r"acls|bls|reanimaci[oó]n", text, re.I):
        requisitos.append("ACLS / BLS vigente")

    req_joined = ", ".join(requisitos)
    local_summary = f"• <b>Modalidad:</b> {turno}\n• <b>Requisitos:</b> {req_joined}"

    if not GROQ_API_KEY:
        return local_summary

    try:
        payload = {
            "model": "llama-3.1-8b-instant",
            "messages": [
                {
                    "role": "system",
                    "content": "Resume en 2 viñetas breves: 1) Turno/Jornada, 2) Requisitos técnicos (SIS, IAAS, ACLS).",
                },
                {"role": "user", "content": text[:800]},
            ],
            "temperature": 0.2,
            "max_tokens": 120,
        }
        resp = session.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )
        if resp.status_code == 200:
            return html.escape(resp.json()["choices"][0]["message"]["content"].strip())
    except Exception:
        pass

    return local_summary


def send_telegram_message(session: requests.Session, text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID en Secrets.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = session.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            return True
        logger.warning(f"Telegram API error {resp.status_code}: {resp.text}")
        return False
    except Exception as e:
        logger.error(f"Error enviando a Telegram: {e}")
        return False


def main():
    logger.info("Iniciando barrido de enfermería en Chile...")
    session = create_session()
    seen_jobs = load_seen_jobs()

    raw_jobs: List[Dict] = []
    raw_jobs.extend(scrape_computrabajo(session))
    raw_jobs.extend(scrape_chiletrabajos(session))
    raw_jobs.extend(scrape_jora_chile(session))
    raw_jobs.extend(scrape_multiportal_rss(session))

    unique_new_jobs: List[Dict] = []
    current_run_ids: Set[str] = set()

    for job in raw_jobs:
        jid = make_job_id(job["title"], job["company"], job["url"])
        if jid in seen_jobs or jid in current_run_ids:
            continue
        current_run_ids.add(jid)
        job["id"] = jid
        job["score"] = score_job(job)
        unique_new_jobs.append(job)

    unique_new_jobs.sort(key=lambda x: x["score"], reverse=True)
    top_30_jobs = unique_new_jobs[:MAX_JOBS_PER_RUN]

    if not top_30_jobs:
        logger.info("No hay ofertas nuevas en esta corrida.")
        return

    now_cl = datetime.now(timezone(timedelta(hours=-3))).strftime("%d/%m/%Y %H:%M")
    header_msg = (
        f"🏥 <b>RADAR ENFERMERÍA CHILE — TOP {len(top_30_jobs)} OFERTAS</b>\n"
        f"📅 <i>{now_cl} CL</i>"
    )
    send_telegram_message(session, header_msg)
    time.sleep(1.5)

    sent_count = 0
    for idx, job in enumerate(top_30_jobs, start=1):
        analysis = analyze_nurse_offer(job, session)
        title_esc = html.escape(job["title"])
        comp_esc = html.escape(job["company"])
        loc_esc = html.escape(job["location"])
        portal_esc = html.escape(job["portal"])
        desc_short = html.escape((job.get("description") or "")[:200])
        url_clean = html.escape(job["url"], quote=True)

        desc_line = f"📝 <i>{desc_short}...</i>\n\n" if desc_short else "\n"

        msg = (
            f"🩺 <b>#{idx} | {title_esc}</b>\n"
            f"🏢 <b>Institución:</b> {comp_esc}\n"
            f"📍 <b>Ubicación:</b> {loc_esc} | 🌐 {portal_esc}\n\n"
            f"{analysis}\n"
            f"{desc_line}"
            f'🔗 <a href="{url_clean}">Ver oferta y postular</a>'
        )

        if send_telegram_message(session, msg):
            seen_jobs.add(job["id"])
            sent_count += 1
            time.sleep(random.uniform(1.8, 2.5))

    save_seen_jobs(seen_jobs)
    logger.info(f"Proceso finalizado. {sent_count} ofertas enviadas.")


if __name__ == "__main__":
    main()
