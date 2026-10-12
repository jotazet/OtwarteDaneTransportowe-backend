# Polish and EU regulatory and market context

Researched 2026-10-06 for the sister project Solitable and copied here. Laws and programmes change, so re-check sources before relying on a claim in product copy or sales material. Items marked **(unverified)** lack a primary source.

## EU: MMTIS (Delegated Regulation 2017/1926, amended by 2024/490)

- Consolidated text: <https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:02017R1926-20240304>
- **Scope:** the entire transport network, including local and rural bus. Obliged "data holders" are transport authorities, operators, infrastructure managers and demand-responsive transport providers.
- **Static data** (Annex 1.1): operating calendar and day types, stops and their geometry, interchanges, routes, operators, timetables, hours of operation, and accessibility of stops and vehicles.
- **Format** (Art. 4):
  - **NeTEx** (CEN/TS 16614) following an EU or national profile, *or* a format proven "fully compatible and interoperable".
  - GTFS is not named.
- **Deadlines:**
  - Static data for the rest of the network: **1 Dec 2023** (already passed).
  - Realtime (SIRI or compatible) for the rest of the network: **1 Dec 2028**.
- **Updates** (Art. 6): data must be updated when it changes, and changes known in advance must be published in advance.
- **Aggregators:** data may be supplied through an aggregator. Implementation handbook: <https://transport.ec.europa.eu/document/download/0b75db16-35b1-41df-8229-5c8abfec534d_en?filename=MMTIS_implementation_handbook.pdf>
- The ITS Directive amendment 2023/2661 adds a duty to digitise data for the whole network from 31 Dec 2028 (per the Polish UC89 impact assessment).

## Poland

- **National access point (KPD MMTIS):** run by Ministerstwo Infrastruktury. <https://www.gov.pl/web/infrastruktura/krajowy-punkt-dostepu-do-uslug-informacji-o-podrozach-multimodalnych-kpd-mmtis>
  - **Today:** a register of links on dane.gov.pl, updated from Excel submissions. No format is enforced.
  - **Participation:** only about 202 of about 2,500 potential data holders submit anything, and NeTEx is rarely used ([UC89 impact assessment](https://www.zpp.pl/storage/library/2026-01/3518def8afafc37ff0243c61be98e4ac.pdf)).
  - **Target KPD:** built 2026–2029 by MI with NASK-PIB ([project](https://www.gov.pl/web/infrastruktura/projekt-krajowy-punkt-dostepu-do-danych-o-podrozach-multimodalnych-kpd-mmtis)). Planned: stops in 2026, timetables in 2027, realtime in 2028. It will include a validator, a **national stop register**, and a **free converter for small gminy**.
- **Polish NeTEx profile:** "Krajowy profil standardu NeTEx", prepared for MI around 2022, based on EPIP. It recommends an ID structure based on the shared `epd` codespace, mandatory for frame IDs and optional for objects, and defines DayType/ServiceCalendar usage. Spec: <https://www.gov.pl/attachment/7c998979-3031-40c5-aa94-d549883b9c40>
  - Consultation report: <https://www.gov.pl/attachment/a76aa2b7-baf9-41f6-9892-850f385cfdbf>
  - The profile documents publish no XSD or validator. The consultation report recommends that the ministry prepare both (p. 25).
- **Law in force:** the timetable regulation (rozporządzenie w sprawie rozkładów jazdy, 2012; t.j. Dz.U. 2018 poz. 202) governs timetable content, approval and the restriction symbols. **It imposes no GTFS or NeTEx duty today.**
- **Pending legislation:**
  - **UD232** (public transport reform) would have required NeTEx, or GTFS fully interoperable with NeTEx. It was **vetoed on 19/20 Aug 2026**, and new proposals are announced.
  - **UC89** (ITS Directive transposition) would oblige organizers to deliver timetables and fares to KPD in machine-readable form, with checks by GITD. Council of Ministers adoption was planned for Q3 2026. Poland missed the transposition deadline, so **the obligation is likely coming.**
- **Audit:** NIK report P/23/019 (2024) found that online publication of FRPA timetables "zależy jedynie od dobrej woli organizatora i operatora", and that about 40% of gminy and powiaty failed to submit their statutory reports. <https://www.nik.gov.pl/kontrole/wyniki-kontroli-nik/pobierz,kin~p_23_019_202408221412081724328728~id0~01,typ,kj.pdf>

## Market: FRPA ("PKS+")

- **Funding:** up to 3.00 zł per vehicle-km, with at least 10% own contribution. **9,039 lines** were funded in Q1 2026, with a 2026 budget of 1.235 bn zł. <https://www.gov.pl/web/infrastruktura/fundusz-rozwoju-przewozow-autobusowych>
- **Organizers:** no national figure exists. Regional samples:
  - Zachodniopomorskie: 77 (69 gminy, 8 powiaty)
  - Mazowieckie: 79
  - Kujawsko-pomorskie: 66
  - Podkarpackie: 42
  - Lubuskie: 20

  Extrapolated total: **about 700–1,000 organizers (unverified)**.
- **Data requirements:** FRPA imposes no data-publication requirement today.

## Privacy: request logs of the journey planner

Researched 2026-10-12 for [ADR 0010](../architecture/decisions/0010-public-planner-api-access.md). Not legal advice; confirm with ODT's data protection officer (IOD).

- **IP addresses are personal data in most cases.** GDPR Recital 30 names IP addresses among online identifiers ([GDPR, EUR-Lex](https://eur-lex.europa.eu/eli/reg/2016/679/oj)). The CJEU held that a dynamic IP address stored by a website operator is personal data when the operator has legal means to identify the person through the access provider ([C-582/14 *Breyer*, 19 Oct 2016](https://curia.europa.eu/juris/document/document.jsf?docid=184668&doclang=EN)).
- **Security as a purpose.** Recital 49 recognises processing strictly necessary for network and information security, e.g. preventing denial-of-service attacks, as a legitimate interest.
- **Legal basis.** Art. 6(1)(f) (legitimate interests) does not apply to processing by public authorities in the performance of their tasks (Art. 6(1), second subparagraph). If ODT's operator is a public body, the basis is likely Art. 6(1)(e) (public task). **(to confirm with the IOD)**
- **Minimisation and storage limitation.** Art. 5(1)(c) and (e) apply. ODT therefore logs no query parameters (no trip coordinates), keeps raw rows with IPs for 30 days and keeps only aggregates without IPs afterwards. No regulation found sets a specific retention period for API access logs **(unverified that none exists)**.
- **Open item.** The privacy notice of the website and the API terms must describe this log: purpose, retention, legal basis, recipients. Wording belongs to the frontend and the legal review.
