#!/usr/bin/env python3
"""
HøringsSonar v2.0 - Høringsovervåking for Autismeforeningen i Norge (AiN)

Skanner offentlige høringer og politiske prosesser som er relevante for
AiN sitt interessefelt: autisme/nevromangfold, funksjonsnedsettelse,
skole og opplæring, helse- og omsorgstjenester, NAV-ytelser, barnevern,
likestilling/CRPD, arbeidsliv og bolig.

Kilder:
- Stortinget (høringsliste for komiteene + representantforslag)
- Regjeringen.no (alle departementers høringer, samlet RSS)
- Utdanningsdirektoratet (Udir)
- Helsedirektoratet (ingen RSS - HTML-scraping)
- Bufdir - Barne-, ungdoms- og familiedirektoratet (ingen RSS - HTML-scraping)

Legge til en ny kilde:
- Har kilden RSS? Legg til en oppføring i KILDER_RSS - ferdig.
- Har kilden ikke RSS? Skriv en liten parser-funksjon etter mønster av
  `_skann_helsedirektoratet` / `_skann_bufdir`, og registrer den i
  `_html_scannere()`.
"""

import os
import json
import smtplib
import re
import asyncio
import aiohttp
import logging
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dataclasses import dataclass, field, asdict
from typing import Optional
from collections import Counter
from bs4 import BeautifulSoup
import feedparser

# --- KONFIGURASJON ---

@dataclass
class Signal:
    """Et høringssignal (høring, representantforslag, proposisjon, NOU m.m.)."""
    type: str  # "horing", "representantforslag", "proposisjon", "nyhet"
    kilde: str
    kategori: str
    tittel: str
    url: str
    sammendrag: str = ""
    keywords: list = field(default_factory=list)
    prioritet: int = 3  # 1=Kritisk, 2=Viktig, 3=Info
    sannsynlighet: str = "Ukjent"  # Høy/Medium/Lav
    konsekvens: str = "Ukjent"  # Høy/Medium/Lav
    tidshorisont: str = "Ukjent"  # <1år, 1-3år, >3år
    deadline: str = ""
    publisert: Optional[date] = None

    def __post_init__(self):
        """Beregn prioritet."""
        # Høy prioritet hvis høring med frist
        if self.type == "horing" and self.deadline:
            self.prioritet = 1
        # Høy prioritet hvis kritiske nøkkelord
        kritiske = {
            "frist", "høringsfrist", "ikrafttredelse", "krav", "forbud",
            "pålegg", "kutt", "innstramming", "tvang"
        }
        if any(k in str(self.keywords).lower() for k in kritiske):
            self.prioritet = min(self.prioritet, 2)


# RSS-kilder - ren konfigurasjon, legg til nye ved å legge til en oppføring
KILDER_RSS = {
    "regjeringen_horinger": {
        "url": "https://www.regjeringen.no/no/rss/Rss/2581966/?documentType=dokumenter/h%C3%B8ringer",
        "dokumenttype": "horing",
        "institusjon": "Regjeringen.no (alle departement)"
    },
    "stortinget_horingsliste": {
        "url": "https://www.stortinget.no/no/Stottemeny/RSS/Horingsliste/",
        "dokumenttype": "horing",
        "institusjon": "Stortinget - høringsliste"
    },
    "stortinget_representantforslag": {
        "url": "https://www.stortinget.no/no/Stottemeny/RSS/Representantforslag/",
        "dokumenttype": "representantforslag",
        "institusjon": "Stortinget - representantforslag"
    },
    "udir_horinger": {
        "url": "https://hoering.udir.no/rss",
        "dokumenttype": "horing",
        "institusjon": "Utdanningsdirektoratet (Udir)"
    }
}

# Nøkkelord organisert etter AiN sine fokusområder (brede termer for bedre treff)
FOKUSOMRÅDER = {
    "autisme_og_nevromangfold": [
        "autisme", "autismespekter", "asperger", "asf", "nevroutviklingsforstyrrelse",
        "nevromangfold", "nevrodivergent", "adhd", "tourette", "utviklingshemming",
        "utviklingshemmet", "kognitiv funksjonsnedsettelse"
    ],
    "skole_og_opplæring": [
        "spesialundervisning", "spesialpedagogisk", "tilrettelagt opplæring",
        "individuell opplæringsplan", "iop", "opplæringsloven", "sakkyndig vurdering",
        "ppt", "pedagogisk-psykologisk tjeneste", "skolefravær", "fraværsgrense",
        "barnehage", "skolemiljø", "mobbing", "spesialskole"
    ],
    "helse_og_omsorgstjenester": [
        "pasientrettigheter", "helse- og omsorgstjenesteloven", "tvang og makt",
        "kapittel 9", "kapittel 25", "habilitering", "rehabilitering", "avlastning",
        "omsorgsstønad", "individuell plan", "koordinator", "fastlege",
        "psykisk helsevern", "psykisk helse", "rusbehandling", "diagnose",
        "diagnostisering"
    ],
    "nav_og_ytelser": [
        "brukerstyrt personlig assistanse", "bpa", "pleiepenger", "hjelpestønad",
        "grunnstønad", "omsorgspenger", "arbeidsavklaringspenger", "uføretrygd",
        "folketrygdloven"
    ],
    "barnevern_og_familie": [
        "barnevernsloven", "barnevernstjenesten", "omsorgsovertakelse", "fosterhjem",
        "familievern"
    ],
    "likestilling_og_rettigheter": [
        "diskriminering", "tilgjengelighet", "universell utforming",
        "likestillings- og diskrimineringsloven", "crpd", "fn-konvensjonen",
        "menneskerettigheter", "vergemål"
    ],
    "arbeid_og_inkludering": [
        "inkluderende arbeidsliv", "ia-avtalen", "tilrettelegging i arbeidslivet",
        "arbeidsrettede tiltak", "varig tilrettelagt arbeid", "vta", "sysselsetting"
    ],
    "bolig": [
        "tilrettelagt bolig", "husbanken", "boligtilskudd", "kommunal bolig",
        "omsorgsbolig"
    ]
}

ALLE_KEYWORDS = []
for kategori_keywords in FOKUSOMRÅDER.values():
    ALLE_KEYWORDS.extend(kategori_keywords)

# Matcher hele ord/fraser (ikke delstrenger) - forhindrer at f.eks. "vta" treffer
# inni "avtale", eller at korte forkortelser treffer tilfeldig inni andre ord.
KEYWORD_PATTERNS = {
    kw: re.compile(r'\b' + re.escape(kw) + r'\b', re.IGNORECASE | re.UNICODE)
    for kw in ALLE_KEYWORDS
}


def finn_matchende_keywords(tekst: str, keywords: list) -> list:
    """Returner hvilke av `keywords` som finnes som hele ord/fraser i teksten."""
    return [kw for kw in keywords if KEYWORD_PATTERNS[kw].search(tekst)]

CONFIG = {
    "cache_file": "horingssonar_cache.json",
    "request_timeout": 30,
    "retry_attempts": 3,
    "retry_delay": 2,
    "rate_limit_delay": 0.5,
    "max_entries": 30,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("HøringsSonar")

# Norske måneder
NORWEGIAN_MONTHS = {
    "januar": 1, "februar": 2, "mars": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "desember": 12
}


# --- HJELPEFUNKSJONER ---

def parse_norsk_dato(text: str) -> Optional[date]:
    """Parser norske datoformater."""
    if not text:
        return None
    text_lower = text.lower()

    # dd.mm.yyyy
    m1 = re.search(r'\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b', text_lower)
    if m1:
        try:
            d, m, y = map(int, m1.groups())
            return date(y, m, d)
        except ValueError:
            pass

    # d. måned yyyy
    m2 = re.search(r'\b(\d{1,2})\.\s*([a-zæøå]+)\s+(\d{4})\b', text_lower)
    if m2:
        try:
            d = int(m2.group(1))
            month_word = m2.group(2)
            y = int(m2.group(3))
            month_num = NORWEGIAN_MONTHS.get(month_word)
            if month_num:
                return date(y, month_num, d)
        except ValueError:
            pass

    return None


def ekstraher_deadline(text: str) -> Optional[str]:
    """Finn frister/deadlines."""
    if not text:
        return None

    patterns = [
        r'(høringsfrist|frist)\s*[:\-]?\s*\d{1,2}\.\d{1,2}\.\d{4}',
        r'(trer i kraft|ikrafttredelse)\s*[:\-]?\s*\d{1,2}\.\d{1,2}\.\d{4}',
        r'(høringsfrist|frist)\s*[:\-]?\s*\d{1,2}\.\s*[a-zæøå]+\s+\d{4}',
        r'(senest|innen)\s+\d{1,2}\.\s*[a-zæøå]+\s+\d{4}',
    ]

    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(0)

    return None


def estimat_tidshorisont(text: str, publisert: Optional[date]) -> str:
    """Estimat når saken kan tre i kraft/avgjøres, relativt til inneværende år."""
    if not text:
        return "Ukjent"

    text_lower = text.lower()
    naa = datetime.now().year

    # Sjekk eksplisitte årstall i teksten, relativt til inneværende år
    m = re.search(r'\b(20\d{2})\b', text_lower)
    if m:
        diff = int(m.group(1)) - naa
        if diff <= 1:
            return "<1 år"
        if diff <= 3:
            return "1-3 år"
        return ">3 år"

    if re.search(r'(umiddelbar|straks|med virkning fra)', text_lower):
        return "<1 år"

    # Basert på type dokument
    if "høring" in text_lower or "forslag" in text_lower:
        return "1-3 år"
    if "nou" in text_lower or "utredning" in text_lower:
        return ">3 år"

    return "Ukjent"


def vurder_sannsynlighet(text: str, dokumenttype: str) -> str:
    """Vurder sannsynlighet for at forslaget blir vedtatt/gjennomført."""
    if not text:
        return "Ukjent"

    text_lower = text.lower()

    # Høy sannsynlighet
    if dokumenttype == "proposisjon" or "regjeringen foreslår" in text_lower:
        return "Høy"

    # Medium sannsynlighet
    if dokumenttype == "horing" or "høring" in text_lower:
        return "Medium"

    # Lav sannsynlighet
    if dokumenttype == "representantforslag" or "representantforslag" in text_lower:
        return "Lav"

    return "Ukjent"


def vurder_konsekvens(keywords: list, kategori: str) -> str:
    """Vurder konsekvens for AiN sine medlemmer og interessefelt."""
    kw_str = " ".join(keywords).lower()

    # Høy konsekvens uansett kategori hvis inngripende nøkkelord
    hoye_impact = ["forbud", "krav", "pålegg", "kutt", "innstramming", "tvang", "rettighet"]
    if any(k in kw_str for k in hoye_impact):
        return "Høy"

    # Direkte tjenester/ytelser for målgruppen er ofte høy konsekvens
    if kategori in ["helse_og_omsorgstjenester", "nav_og_ytelser", "skole_og_opplæring",
                     "barnevern_og_familie", "autisme_og_nevromangfold"]:
        return "Høy"

    # Medium konsekvens
    if kategori in ["likestilling_og_rettigheter", "arbeid_og_inkludering", "bolig"]:
        return "Medium"

    return "Lav"


def format_prioritet(prioritet: int) -> str:
    return {
        1: "🔴 Kritisk",
        2: "🟠 Viktig",
        3: "🟢 Info"
    }.get(prioritet, "🟢 Info")


def foreslå_handling(signal: Signal) -> str:
    """Foreslå handling for AiN på signalet."""
    if signal.deadline:
        return "Vurder høringssvar fra AiN - meld ansvarlig i interessepolitisk arbeid denne uken."

    if signal.sannsynlighet == "Høy" and signal.konsekvens == "Høy":
        return "Kritisk - løft saken til interessepolitisk utvalg umiddelbart."

    if signal.type == "representantforslag":
        return "Tidlig signal fra Stortinget - følg saken videre, ingen handling påkrevd ennå."

    handlinger_per_kategori = {
        "skole_og_opplæring": "Vurder innspill om spesialundervisning/tilrettelagt opplæring.",
        "nav_og_ytelser": "Sjekk konsekvens for BPA, pleiepenger, hjelpestønad og lignende ytelser.",
        "helse_og_omsorgstjenester": "Vurder konsekvens for helsetjenester og bruk av tvang og makt.",
        "barnevern_og_familie": "Vurder konsekvens for familier med autisme i møte med barnevernet.",
        "likestilling_og_rettigheter": "Vurder opp mot CRPD og likestillings- og diskrimineringsloven.",
        "arbeid_og_inkludering": "Vurder betydning for sysselsetting og tilrettelegging i arbeidslivet.",
        "bolig": "Vurder betydning for bolig- og boformtilbud.",
        "autisme_og_nevromangfold": "Direkte relevant for autisme/nevromangfold - prioriter gjennomgang."
    }
    if signal.kategori in handlinger_per_kategori:
        return handlinger_per_kategori[signal.kategori]

    if signal.konsekvens == "Høy":
        return "Analyser konsekvens for AiN sine medlemmer."

    return "Bevar i oversikten - følg med på utvikling."


# --- HOVEDMOTOR ---

class HoringsSonar:
    def __init__(self):
        self.cache = self._last_cache()
        self.signaler = []
        self.feil = []

    def _last_cache(self) -> dict:
        if os.path.exists(CONFIG["cache_file"]):
            try:
                with open(CONFIG["cache_file"], 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Kunne ikke laste cache: {e}")
        return {"sett_urls": [], "siste_kjoring": None}

    def _lagre_cache(self):
        self.cache["siste_kjoring"] = datetime.now().isoformat()
        try:
            with open(CONFIG["cache_file"], 'w', encoding='utf-8') as f:
                json.dump(self.cache, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Kunne ikke lagre cache: {e}")

    async def _fetch_med_retry(self, session: aiohttp.ClientSession, url: str) -> Optional[str]:
        for attempt in range(CONFIG["retry_attempts"]):
            try:
                async with session.get(url, timeout=CONFIG["request_timeout"]) as response:
                    if response.status == 200:
                        return await response.text()
                    elif response.status == 429:
                        await asyncio.sleep(CONFIG["retry_delay"] * (attempt + 1))
                    else:
                        logger.warning(f"HTTP {response.status} for {url}")
                        return None
            except asyncio.TimeoutError:
                logger.warning(f"Timeout for {url} (forsøk {attempt + 1})")
            except Exception as e:
                logger.error(f"Feil ved {url}: {e}")

            if attempt < CONFIG["retry_attempts"] - 1:
                await asyncio.sleep(CONFIG["retry_delay"])

        return None

    def _vurder_og_legg_til(self, tittel: str, fritekst: str, link: str,
                             kilde: str, dokumenttype: str,
                             deadline_hint: Optional[str] = None):
        """Felles vurderingslogikk brukt av både RSS- og HTML-kilder."""
        if not link or not tittel:
            return
        if link in self.cache.get("sett_urls", []):
            return

        tekst = f"{tittel} {fritekst}".lower()
        matchende_keywords = finn_matchende_keywords(tekst, ALLE_KEYWORDS)
        if not matchende_keywords:
            return

        kategorier = [kat for kat, kws in FOKUSOMRÅDER.items() if finn_matchende_keywords(tekst, kws)]
        hovedkategori = kategorier[0] if kategorier else "generelt"

        deadline = deadline_hint or ekstraher_deadline(fritekst) or ekstraher_deadline(tittel) or ""
        sannsynlighet = vurder_sannsynlighet(tekst, dokumenttype)
        konsekvens = vurder_konsekvens(matchende_keywords, hovedkategori)
        tidshorisont = estimat_tidshorisont(tekst, None)

        self.signaler.append(Signal(
            type=dokumenttype,
            kilde=kilde,
            kategori=hovedkategori,
            tittel=tittel,
            url=link,
            sammendrag=fritekst[:300],
            keywords=matchende_keywords[:8],
            deadline=deadline,
            sannsynlighet=sannsynlighet,
            konsekvens=konsekvens,
            tidshorisont=tidshorisont
        ))

        self.cache.setdefault("sett_urls", []).append(link)
        logger.info(f"Nytt signal [{kilde}]: {tittel[:60]}...")

    async def _skann_rss_kilder(self, session: aiohttp.ClientSession):
        """Skann alle RSS-kilder i KILDER_RSS."""
        logger.info(f"Skanner {len(KILDER_RSS)} RSS-kilder...")

        for navn, kilde_config in KILDER_RSS.items():
            await asyncio.sleep(CONFIG["rate_limit_delay"])

            xml = await self._fetch_med_retry(session, kilde_config["url"])
            if not xml:
                self.feil.append(f"Kunne ikke hente RSS: {navn}")
                continue

            try:
                feed = feedparser.parse(xml)
                for entry in feed.entries[:CONFIG["max_entries"]]:
                    tittel = getattr(entry, 'title', '')
                    sammendrag = getattr(entry, 'summary', '')
                    link = getattr(entry, 'link', '')

                    self._vurder_og_legg_til(
                        tittel=tittel,
                        fritekst=sammendrag,
                        link=link,
                        kilde=kilde_config["institusjon"],
                        dokumenttype=kilde_config["dokumenttype"]
                    )
            except Exception as e:
                logger.error(f"Feil ved parsing av {navn}: {e}")
                self.feil.append(f"Feil ved parsing av {navn}: {e}")

    async def _skann_helsedirektoratet(self, session: aiohttp.ClientSession):
        """Helsedirektoratet har ingen RSS - vi scraper høringssiden direkte."""
        url = "https://www.helsedirektoratet.no/horinger"
        html = await self._fetch_med_retry(session, url)
        if not html:
            self.feil.append("Kunne ikke hente Helsedirektoratet høringer")
            return

        try:
            soup = BeautifulSoup(html, "lxml")
            for li in soup.select("li.b-nav-list__item"):
                a = li.select_one("a.b-nav-list__link")
                if not a:
                    continue
                link = a.get("href", "")
                fulltekst = a.get_text(" ", strip=True)
                # Tittelen er teksten før "Høringsfrist" / dato-informasjon
                tittel = re.split(r'\s*Høringsfrist', fulltekst, flags=re.IGNORECASE)[0].strip()
                self._vurder_og_legg_til(
                    tittel=tittel,
                    fritekst=fulltekst,
                    link=link,
                    kilde="Helsedirektoratet",
                    dokumenttype="horing"
                )
        except Exception as e:
            logger.error(f"Feil ved parsing av Helsedirektoratet: {e}")
            self.feil.append(f"Feil ved parsing av Helsedirektoratet: {e}")

    async def _skann_bufdir(self, session: aiohttp.ClientSession):
        """Bufdir har ingen RSS - vi scraper høringssiden direkte."""
        url = "https://www.bufdir.no/hoeringer"
        html = await self._fetch_med_retry(session, url)
        if not html:
            self.feil.append("Kunne ikke hente Bufdir høringer")
            return

        try:
            soup = BeautifulSoup(html, "lxml")
            for li in soup.select("div.bd-block-link-list li"):
                a = li.find("a", href=True)
                if not a:
                    continue
                link = a["href"]
                tittel_el = a.select_one(".bd-fw-semibold")
                frist_el = a.select_one(".bd-fw-normal")
                tittel = tittel_el.get_text(strip=True) if tittel_el else a.get_text(strip=True)
                fristtekst = frist_el.get_text(strip=True) if frist_el else ""
                self._vurder_og_legg_til(
                    tittel=tittel,
                    fritekst=f"{tittel} {fristtekst}",
                    link=link,
                    kilde="Bufdir",
                    dokumenttype="horing"
                )
        except Exception as e:
            logger.error(f"Feil ved parsing av Bufdir: {e}")
            self.feil.append(f"Feil ved parsing av Bufdir: {e}")

    def _html_scannere(self):
        """Registrer alle egendefinerte (ikke-RSS) skannere her."""
        return [
            self._skann_helsedirektoratet,
            self._skann_bufdir,
        ]

    async def kjor_skanning(self) -> dict:
        logger.info("=" * 70)
        logger.info("HøringsSonar v2.0 - Starter høringsovervåking for AiN")
        logger.info("=" * 70)

        headers = {"User-Agent": CONFIG["user_agent"]}
        connector = aiohttp.TCPConnector(limit=5)

        async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
            await self._skann_rss_kilder(session)
            for scanner in self._html_scannere():
                await asyncio.sleep(CONFIG["rate_limit_delay"])
                await scanner(session)

        self._lagre_cache()

        rapport = {
            "tidspunkt": datetime.now().isoformat(),
            "signaler": [asdict(s) for s in self.signaler],
            "feil": self.feil,
            "statistikk": {
                "signaler_funnet": len(self.signaler),
                "kilder_sjekket": len(KILDER_RSS) + len(self._html_scannere())
            }
        }

        logger.info("-" * 70)
        logger.info(f"Skanning fullført: {len(self.signaler)} nye signaler")

        return rapport


# --- RAPPORTER ---

def generer_markdown_rapport(rapport: dict) -> str:
    """Generer Markdown-rapport for AiN."""
    now = datetime.now()
    uke = now.isocalendar().week

    signaler = [Signal(**s) for s in rapport["signaler"]]
    stats = rapport["statistikk"]

    # Sorter etter prioritet
    kritisk = [s for s in signaler if s.prioritet == 1]
    viktig = [s for s in signaler if s.prioritet == 2]
    info = [s for s in signaler if s.prioritet == 3]

    # Frister
    frister = []
    for s in signaler:
        if s.deadline:
            dato = parse_norsk_dato(s.deadline)
            frister.append((dato, s))
    frister.sort(key=lambda x: (x[0] is None, x[0] or date.max))

    # Statistikk
    per_kategori = Counter(s.kategori for s in signaler)
    per_type = Counter(s.type for s in signaler)
    per_konsekvens = Counter(s.konsekvens for s in signaler)

    lines = []
    lines.append("# 📡 HøringsSonar - Ukesrapport for AiN")
    lines.append(f"**Uke {uke}, {now.year}** | Generert: {now.strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append("## 📊 Sammendrag")
    lines.append(f"- **Nye signaler:** {stats['signaler_funnet']}")
    lines.append(f"- **Prioritering:** 🔴 {len(kritisk)} | 🟠 {len(viktig)} | 🟢 {len(info)}")
    lines.append(f"- **Høringer med frist:** {len(frister)}")
    lines.append("")

    # Vurderingsmatrise
    lines.append("## 🎯 Vurdering")
    lines.append("| Signal | Sannsynlighet | Konsekvens | Tidshorisont |")
    lines.append("|--------|---------------|-----------|--------------|")
    for s in sorted(signaler, key=lambda x: (x.prioritet, x.konsekvens))[:10]:
        lines.append(f"| {s.tittel[:40]} | {s.sannsynlighet} | {s.konsekvens} | {s.tidshorisont} |")
    lines.append("")

    # Topp handlinger
    lines.append("## 💡 Anbefalte Handlinger (Topp 5)")
    topp_signaler = sorted(signaler, key=lambda x: (x.prioritet, -ord(x.konsekvens[0])))[:5]
    if topp_signaler:
        for idx, s in enumerate(topp_signaler, 1):
            pri = format_prioritet(s.prioritet)
            handling = foreslå_handling(s)
            lines.append(f"{idx}. **{s.tittel[:80]}**")
            lines.append(f"   - {pri} | Konsekvens: {s.konsekvens} | Sannsynlighet: {s.sannsynlighet}")
            lines.append(f"   - Handling: {handling}")
            lines.append(f"   - [Les mer]({s.url})")
            lines.append("")
    else:
        lines.append("- Ingen nye signaler denne uken.")
    lines.append("")

    # Frister
    if frister:
        lines.append("## ⏰ Høringer med Frist")
        lines.append("| Frist | Tittel | Kilde |")
        lines.append("|-------|--------|-------|")
        for dato, s in frister[:10]:
            dato_txt = dato.isoformat() if dato else s.deadline
            lines.append(f"| {dato_txt} | {s.tittel[:50]} | {s.kilde} |")
        lines.append("")

    # Tematisk fordeling
    if per_kategori:
        lines.append("## 📈 Fordeling per Fokusområde")
        for kat, antall in per_kategori.most_common():
            lines.append(f"- **{kat.replace('_', ' ').title()}**: {antall} signaler")
        lines.append("")

    # Detaljliste
    lines.append("## 📋 Detaljert Signalliste")
    for seksjon, items in [("🔴 Kritiske", kritisk), ("🟠 Viktige", viktig), ("🟢 Info", info)]:
        if items:
            lines.append(f"### {seksjon}")
            for s in items[:15]:
                kws = ", ".join(s.keywords[:5])
                dl = f" | Frist: {s.deadline}" if s.deadline else ""
                lines.append(f"- **[{s.type.upper()}] {s.tittel}**")
                lines.append(f"  - Kilde: {s.kilde}{dl}")
                lines.append(f"  - Vurdering: {s.sannsynlighet} sannsynlighet, {s.konsekvens} konsekvens, {s.tidshorisont}")
                lines.append(f"  - Nøkkelord: {kws}")
                lines.append(f"  - Handling: {foreslå_handling(s)}")
                lines.append(f"  - [Les dokumentet]({s.url})")
                lines.append("")

    if rapport.get("feil"):
        lines.append("## ⚠️ Kilder som feilet denne kjøringen")
        for f in rapport["feil"]:
            lines.append(f"- {f}")
        lines.append("")

    lines.append("---")
    lines.append("*HøringsSonar v2.0 | Høringsovervåking for Autismeforeningen i Norge (AiN)*")

    return "\n".join(lines)


def send_epost_rapport(rapport: dict, markdown: str):
    """Send e-post med rapport."""
    bruker = os.environ.get("EMAIL_USER", "").strip()
    passord = os.environ.get("EMAIL_PASS", "").strip()
    mottaker = os.environ.get("EMAIL_RECIPIENT", "").strip() or bruker

    if not all([bruker, passord, mottaker]):
        logger.warning("E-postkonfigurasjon mangler.")
        return False

    if not rapport["signaler"]:
        logger.info("Ingen nye signaler - hopper over e-post.")
        return False

    msg = MIMEMultipart("alternative")
    uke = datetime.now().isocalendar().week
    n_signaler = rapport['statistikk']['signaler_funnet']

    signaler_obj = [Signal(**s) for s in rapport["signaler"]]
    kritisk = len([s for s in signaler_obj if s.prioritet == 1])
    viktig = len([s for s in signaler_obj if s.prioritet == 2])

    emne = f"📡 HøringsSonar uke {uke}: "
    if kritisk > 0:
        emne += f"🔴 {kritisk} kritisk, "
    if viktig > 0:
        emne += f"🟠 {viktig} viktig, "
    emne += f"{n_signaler} nye signaler"

    msg["Subject"] = emne
    msg["From"] = bruker
    msg["To"] = mottaker

    # Tekst-versjon
    tekst_versjon = markdown.replace("**", "").replace("##", "").replace("#", "")
    msg.attach(MIMEText(tekst_versjon, "plain", "utf-8"))

    # HTML-versjon (enkel)
    html = f"<html><body><pre style='font-family:Arial,sans-serif;white-space:pre-wrap'>{markdown}</pre></body></html>"
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(bruker, passord)
            server.sendmail(bruker, [mottaker], msg.as_string())
        logger.info(f"Rapport sendt til {mottaker}")
        return True
    except Exception as e:
        logger.error(f"E-postfeil: {e}")
        return False


# --- HOVEDPROGRAM ---

async def main():
    sonar = HoringsSonar()
    rapport = await sonar.kjor_skanning()

    # Lagre JSON
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    rapport_fil_json = f"horingssonar_rapport_{timestamp}.json"
    with open(rapport_fil_json, 'w', encoding='utf-8') as f:
        json.dump(rapport, f, indent=2, ensure_ascii=False)
    logger.info(f"JSON rapport lagret: {rapport_fil_json}")

    # Generer og lagre Markdown
    markdown = generer_markdown_rapport(rapport)
    rapport_fil_md = f"horingssonar_rapport_{timestamp}.md"
    with open(rapport_fil_md, 'w', encoding='utf-8') as f:
        f.write(markdown)
    logger.info(f"Markdown rapport lagret: {rapport_fil_md}")

    # Send e-post
    send_epost_rapport(rapport, markdown)

    return rapport


if __name__ == "__main__":
    asyncio.run(main())
