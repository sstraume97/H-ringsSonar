# 📡 HøringsSonar

HøringsSonar er et automatisert varslingsverktøy for **Autismeforeningen i Norge (AiN)**. Det skanner offentlige høringer og politiske prosesser hver uke, filtrerer ut det som er relevant for AiN sitt interessefelt, og sender en samlet rapport på e-post og som fil i dette repoet.

> Verktøyet startet som en pilot ("LovSonar") for en helt annen bransje. Fra og med v2.0 er det bygget om og døpt om for å dekke AiN sitt behov: å ikke gå glipp av høringer som gjelder autisme, nevromangfold og funksjonsnedsettelse.

## 🕵️ Hva skanner verktøyet?

| Kilde | Hva den dekker | Teknikk |
|---|---|---|
| [Regjeringen.no](https://www.regjeringen.no/no/dokumenter/hoyringar/id1763/) | Alle høringer fra alle departementer (Helse- og omsorgsdep., Barne- og familiedep., Kunnskapsdep., Arbeids- og inkluderingsdep. m.fl.) | RSS |
| [Stortinget](https://www.stortinget.no/no/Hva-skjer-pa-Stortinget/Horing/) | Høringsliste (planlagte komitéhøringer) + nye representantforslag | RSS |
| [Utdanningsdirektoratet (Udir)](https://hoering.udir.no/) | Høringer og innspillsrunder om regelverk, læreplaner m.m. | RSS |
| [Helsedirektoratet](https://www.helsedirektoratet.no/horinger) | Høringer om veiledere, retningslinjer og regelverk på helsefeltet | HTML-scraping (ingen RSS finnes) |
| [Bufdir](https://www.bufdir.no/hoeringer) | Høringer om regelverk og faglige retningslinjer på barnevern/familie-feltet | HTML-scraping (ingen RSS finnes) |

Alle signaler blir vurdert opp mot AiN sine fokusområder før de tas med i rapporten - se `FOKUSOMRÅDER` i [horingssonar.py](horingssonar.py):

- **Autisme og nevromangfold** - autisme, ASF, ADHD, utviklingshemming m.m.
- **Skole og opplæring** - spesialundervisning, IOP, PPT, skolefravær, barnehage m.m.
- **Helse- og omsorgstjenester** - pasientrettigheter, tvang og makt (kap. 9/25), avlastning, habilitering m.m.
- **NAV og ytelser** - BPA, pleiepenger, hjelpestønad, grunnstønad m.m.
- **Barnevern og familie**
- **Likestilling og rettigheter** - CRPD, universell utforming, diskriminering
- **Arbeid og inkludering** - IA-avtalen, tilrettelegging, VTA
- **Bolig** - tilrettelagt bolig, Husbanken
- **Politiske prosesser** (generelt) - NOU, proposisjon, lovforslag, representantforslag

Et treff på **ett eneste** nøkkelord i tittel/ingress er nok til å bli tatt med, og kategoriseres deretter etter hvilket fokusområde det traff sterkest på. Dette er bevisst bredt, slik at vi heller sorterer bort støy manuelt enn å gå glipp av noe.

## 📄 Rapport

Hver kjøring genererer:
- `horingssonar_rapport_<tidsstempel>.md` - lesbar rapport med sammendrag, vurdering (sannsynlighet/konsekvens/tidshorisont), anbefalte handlinger, frister og full signalliste.
- `horingssonar_rapport_<tidsstempel>.json` - samme data strukturert, til videre bruk (f.eks. import i et regneark eller saksregister).
- En e-post med samme innhold, sendt til adressen i `EMAIL_RECIPIENT` (kun hvis det faktisk finnes nye signaler).

`horingssonar_cache.json` holder styr på hvilke URL-er som allerede er sett og rapportert, slik at samme høring ikke dukker opp uke etter uke. Denne filen committes av workflowen og må ikke slettes manuelt.

## ⚙️ Oppsett (GitHub Secrets)

Kjøringen skjer automatisk hver onsdag kl. 07:00 UTC via [.github/workflows/horingssonar.yml](.github/workflows/horingssonar.yml), eller manuelt via "Run workflow". Den trenger tre secrets i repoet (**Settings → Secrets and variables → Actions**):

| Secret | Beskrivelse |
|---|---|
| `EMAIL_USER` | Avsender-adresse (Gmail) |
| `EMAIL_PASS` | App-passord for avsender-adressen |
| `EMAIL_RECIPIENT` | Hvem som skal motta ukesrapporten (kan være en liste/funksjonspostkasse i AiN) |

## ➕ Legge til en ny kilde

Verktøyet er bygget for at det skal være enkelt å utvide etter hvert som vi finner flere relevante høringsinstanser (f.eks. NAV/Arbeids- og velferdsdirektoratet, Diskriminerings- og likestillingsombudet, Husbanken):

1. **Har kilden RSS?** Legg til en oppføring i `KILDER_RSS` i [horingssonar.py](horingssonar.py) med `url`, `dokumenttype` (`horing`/`proposisjon`/`representantforslag`) og `institusjon` (visningsnavn). Ferdig.
2. **Har kilden ikke RSS?** Skriv en liten `async def _skann_<navn>(self, session)`-metode etter mønster av `_skann_helsedirektoratet` / `_skann_bufdir` (hent HTML, parse med BeautifulSoup, kall `self._vurder_og_legg_til(...)` per treff), og legg metoden til i `_html_scannere()`.

Alle nye kilder får automatisk samme filtrering, prioritering og rapportformat som de eksisterende.

## 🛠 Teknisk status

Stack: Python 3.11, aiohttp, feedparser, BeautifulSoup/lxml, GitHub Actions (schedule + workflow_dispatch).

Kjente begrensninger:
- Helsedirektoratet og Bufdir har ingen RSS/API, så disse to kildene er HTML-scraping og kan i prinsippet slutte å virke hvis sidene deres endrer struktur. Feil ved henting logges i rapportens "Kilder som feilet"-seksjon i stedet for å stoppe hele kjøringen.
- Nøkkelordfiltrering er tekstbasert (norsk), og fanger derfor ikke opp saker som er formulert uten noen av de listede begrepene.
