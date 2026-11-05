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
# CONFIGURACIÓN GENERAL Y LOGGING
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

# Palabras obligatorias (al menos una debe estar en el título)
MUST_INCLUDE_REGEX = re.compile(
    r"\b(enfermer[ao]s?|enfermer[ií]a|eu\b|e\.u\.)\b", re.IGNORECASE
)

# Filtro para descartar ofertas que buscan exclusivamente TENS o áreas no clínicas
EXCLUDE_REGEX = re.compile(
    r"\b(t[eé]cnico\s+en\s+enfermer[ií]a|tens\b|auxiliar\s+de\s+enfermer[ií]a|"
    r"veterinari[ao]|agr[oó]nom[ao]|prevencionista|guardia|aseo|limpieza)\b",
    re.IGNORECASE,
)


# ==========================================
# SESIÓN HTTP ROBUSTA CON RETRY
# ==========================================
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


# ==========================================
# GESTIÓN DE HISTORIAL (seen_jobs.json)
# ==========================================
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


# ==========================================
# SCORING PARA RANKING TOP 30
# ==========================================
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


# ==========================================
# 1. SCRAPER: COMPUTRABAJO CHILE
# ==========================================
def scrape_computrabajo(session: requests.Session) -> List[Dict]:
    jobs = []
    urls = [
